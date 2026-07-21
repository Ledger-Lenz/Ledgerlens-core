"""Atomic coordination between the Horizon cursor and rolling-window checkpoints.

``python cli.py stream`` (see ``cli.py``'s ``stream`` command) previously
advanced two independently-triggered durable checkpoints per trade: a
time-or-count-bounded Horizon cursor flush (:class:`ingestion.checkpoint.
CursorCheckpoint`) and a count-only rolling-window state flush
(:class:`detection.rolling_window.RollingWindowStore`). Because one trigger
was time-bounded and the other was not, sustained throughput below the
event-count threshold let the cursor advance on its timer while the
window-state checkpoint stalled indefinitely — a crash in that gap silently
and permanently dropped trades from the wash-trading detector's rolling
windows.

:class:`StreamCheckpointCoordinator` replaces both triggers with one: the
Horizon cursor and the full rolling-window state are written in a single
SQLite transaction (``BEGIN IMMEDIATE`` / commit-or-rollback). Because SQLite
transactions are atomic, a crash at any point before ``COMMIT`` leaves the
database in exactly its pre-checkpoint state — the cursor can never be
durably ahead of the window state it depends on, by construction rather than
by narrowing a timing window. See ``docs/ingestion.md`` for the full
guarantee and the migration path from the legacy JSON cursor file.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ingestion.checkpoint import CursorCheckpoint, FlushPolicy, validate_cursor
from ingestion.metrics import get_metrics

if TYPE_CHECKING:
    from detection.rolling_window import RollingWindowState, RollingWindowStore

logger = logging.getLogger("ledgerlens.stream_checkpoint")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_checkpoint (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    paging_token    TEXT NOT NULL,
    ledger_sequence INTEGER,
    wallet_count    INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class StreamCheckpointCoordinator:
    """Atomically persists the Horizon cursor together with rolling-window state.

    Thread-safety: not thread-safe by design, matching
    :class:`~detection.rolling_window.RollingWindowState` — intended for use
    from the single-threaded streaming loop (and its signal-driven shutdown
    handler, which runs in the same thread between bytecode instructions).
    """

    def __init__(
        self,
        rolling_store: "RollingWindowStore",
        flush_policy: FlushPolicy,
        legacy_cursor_checkpoint: CursorCheckpoint | None = None,
        now: float | None = None,
    ) -> None:
        self._store = rolling_store
        self._flush_policy = flush_policy
        self._legacy = legacy_cursor_checkpoint
        self._events_since_flush = 0
        self._last_flush_time = time.monotonic() if now is None else now
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._store.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------
    # Load / migrate / detect
    # ------------------------------------------------------------------

    def load_cursor(self, actual_wallet_count: int) -> str | None:
        """Return the durable cursor, or ``None`` when none exists yet.

        *actual_wallet_count* is the wallet count the caller already loaded
        from ``rolling_window_checkpoints`` (via
        :meth:`~detection.rolling_window.RollingWindowStore.load_all`). It is
        compared against the wallet count recorded at the last unified
        checkpoint as a defense-in-depth desync check: under normal
        operation the atomic transaction in :meth:`flush` makes divergence
        impossible, so a mismatch here means the underlying storage was
        altered or corrupted outside of that transaction (e.g. manual
        editing, a filesystem-level fault). It is logged and counted, not
        raised — mirroring how the rest of this checkpoint layer treats
        corruption as a recoverable, operator-visible condition rather than
        a crash.
        """
        with self._store.connect() as conn:
            row = conn.execute(
                "SELECT paging_token, wallet_count FROM stream_checkpoint WHERE id = 1"
            ).fetchone()

        if row is None:
            return self._load_legacy_cursor()

        paging_token, expected_wallet_count = row
        if expected_wallet_count != actual_wallet_count:
            logger.error(
                "Checkpoint desync detected: unified checkpoint recorded %d "
                "wallet window(s) but the rolling-window store loaded %d. "
                "The stream_checkpoint and rolling_window_checkpoints tables "
                "have diverged outside of normal atomic-commit operation. "
                "Manual reconciliation against Horizon ledger history is "
                "recommended before trusting the detector's rolling-window "
                "state.",
                expected_wallet_count,
                actual_wallet_count,
            )
            get_metrics().checkpoint_desync_detected_total.inc()

        try:
            return validate_cursor(paging_token)
        except ValueError as exc:
            logger.warning("Ignoring invalid unified checkpoint cursor: %s", exc)
            return None

    def _load_legacy_cursor(self) -> str | None:
        """Seed the initial cursor from the legacy JSON checkpoint, if any.

        Migration path for upgrading an existing deployment: read once, log
        clearly, and never write to or re-read the legacy file again — the
        first call to :meth:`flush` establishes the unified checkpoint as
        authoritative from then on.
        """
        if self._legacy is None:
            return None
        cursor = self._legacy.load()
        if cursor is not None:
            logger.info(
                "Migrating legacy cursor checkpoint (%s) from %s to the "
                "unified SQLite checkpoint; the legacy file will not be "
                "read again once the first unified checkpoint is written",
                cursor,
                self._legacy.path,
            )
        return cursor

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def on_trade_processed(
        self,
        cursor_token: str,
        window_state: "RollingWindowState",
        ledger_sequence: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Advance the event counter and flush if the policy triggers.

        Mirrors the previous cursor-only trigger's semantics: the counters
        are reset once the policy fires and a flush is attempted, regardless
        of whether the underlying write succeeds, so a persistent failure
        (e.g. disk full) retries at the next flush interval rather than on
        every subsequent trade.

        Returns whether a flush was attempted.
        """
        self._events_since_flush += 1
        now = time.monotonic() if now is None else now
        if not self._flush_policy.should_flush(
            self._events_since_flush, self._last_flush_time, now
        ):
            return False
        self.flush(cursor_token, window_state, ledger_sequence=ledger_sequence)
        self._events_since_flush = 0
        self._last_flush_time = now
        return True

    def flush(
        self,
        cursor_token: str,
        window_state: "RollingWindowState",
        ledger_sequence: int | None = None,
    ) -> None:
        """Atomically persist *cursor_token* and every wallet in *window_state*.

        A single ``BEGIN IMMEDIATE`` transaction covers every wallet upsert
        and the cursor upsert; either all of it lands durably or none of it
        does. On failure this logs and returns rather than raising, so the
        streaming loop keeps running on the prior durable checkpoint and
        retries at the next flush — the same fault-tolerance philosophy as
        :meth:`ingestion.checkpoint.CursorCheckpoint.save`.
        """
        try:
            token = validate_cursor(cursor_token)
        except ValueError as exc:
            logger.warning("Refusing to checkpoint malformed cursor: %s", exc)
            return

        wallet_count = window_state.active_wallets
        recorded_at = datetime.now(timezone.utc).isoformat()
        try:
            with self._store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._store.save_all(window_state, conn=conn)
                    conn.execute(
                        """
                        INSERT INTO stream_checkpoint
                            (id, paging_token, ledger_sequence, wallet_count, updated_at)
                        VALUES (1, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            paging_token    = excluded.paging_token,
                            ledger_sequence = excluded.ledger_sequence,
                            wallet_count    = excluded.wallet_count,
                            updated_at      = excluded.updated_at
                        """,
                        (token, ledger_sequence, wallet_count, recorded_at),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except (OSError, sqlite3.Error) as exc:
            logger.error("Failed to write unified stream checkpoint: %s", exc)

    def reset(self) -> None:
        """Delete the unified checkpoint row (used by ``stream --reset-cursor``)."""
        with self._store.connect() as conn:
            conn.execute("DELETE FROM stream_checkpoint WHERE id = 1")
            conn.commit()
