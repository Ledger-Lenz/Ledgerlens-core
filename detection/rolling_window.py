"""Stateful rolling-window trade store for incremental per-wallet feature computation.

Maintains per-wallet deques of trades within a 24-hour horizon.  On each
new trade, expired entries (older than 24 h) are evicted before the trade
is appended.  Callers can then query sub-windows (1 h, 4 h, 24 h) for
feature engineering.

Persistence is handled by :class:`RollingWindowStore`, which serialises
window state to the ``rolling_window_checkpoints`` SQLite table so the
streamer survives restarts without losing accumulated history.

Security notes
--------------
- Checkpoint JSON is produced via ``trade.model_dump()`` (Pydantic), not
  pickle, preventing code execution on load.
- A hard cap of ``MAX_TRADES_PER_WALLET_WINDOW`` trades per wallet protects
  against unbounded memory growth from high-volume accounts.
- Checkpoint contents must not be exposed via the public API.

Window-tolerance contract
-------------------------
Events are keyed on ``trade.ledger_close_time`` and may arrive out of order
or late (network retries, backfill).  :meth:`WalletWindow.add` handles them
as follows:

- **In order** (``ledger_close_time`` >= newest trade in the window):
  appended.
- **Out of order, within tolerance** (older than the newest trade but no
  older than ``WINDOW_TOLERANCE_HOURS`` (24 h) before *now*): late-merged
  into its chronological position, so sub-window queries include it.
  Counted as ``reason="out_of_order"``.
- **Beyond tolerance** (older than ``now - WINDOW_TOLERANCE_HOURS``):
  rejected -- not stored, ``add`` returns ``False``.  Counted as
  ``reason="beyond_window"``.

Both cases increment the ``ledgerlens_rolling_window_late_events_total``
Prometheus counter (labelled by ``reason``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Iterator, List, Optional

from config.settings import settings
from ingestion.data_models import Asset, Trade, TradeType

logger = logging.getLogger("ledgerlens.rolling_window")

WINDOW_HOURS = [1, 4, 24]
WINDOW_TOLERANCE_HOURS = 24
MAX_TRADES_PER_WALLET_WINDOW = 10_000

# Prometheus metric (lazy import to avoid hard dependency)
_late_events_counter = None


def _get_late_events_counter():
    global _late_events_counter
    if _late_events_counter is not None:
        return _late_events_counter
    try:
        from prometheus_client import Counter

        _late_events_counter = Counter(
            "ledgerlens_rolling_window_late_events_total",
            "Trades delivered out of order or beyond the rolling-window tolerance",
            ["reason"],
        )
    except ImportError:
        _late_events_counter = None
    return _late_events_counter


def _record_late_event(reason: str) -> None:
    counter = _get_late_events_counter()
    if counter is not None:
        counter.labels(reason=reason).inc()

_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rolling_window_checkpoints (
    wallet      TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    last_score  INTEGER,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet)
);
"""


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


class WalletWindow:
    """Per-wallet deque of trades covering up to 24 hours.

    Trades are kept in chronological order.  On each :meth:`add`, trades
    older than 24 h are evicted from the left.  A hard cap of
    :data:`MAX_TRADES_PER_WALLET_WINDOW` entries prevents unbounded growth;
    the oldest trade is dropped when the cap is reached and a WARNING is
    logged.

    The ``_last_score`` field caches the most-recently emitted score so
    :class:`~detection.model_inference.IncrementalScorer` can compute the
    delta without querying storage.
    """

    def __init__(self) -> None:
        self._trades: Deque[Trade] = deque()
        self._last_score: Optional[int] = None
        self._last_scored_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add(self, trade: Trade) -> bool:
        """Add *trade* per the window-tolerance contract and evict stale entries.

        Returns ``False`` when the trade is rejected as beyond tolerance.
        """
        self._evict(hours=WINDOW_TOLERANCE_HOURS)
        trade_time = _as_utc(trade.ledger_close_time)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_TOLERANCE_HOURS)
        if trade_time < cutoff:
            _record_late_event("beyond_window")
            logger.debug("WalletWindow: rejecting trade beyond window tolerance (%s)", trade_time)
            return False
        if len(self._trades) >= MAX_TRADES_PER_WALLET_WINDOW:
            logger.warning(
                "WalletWindow cap (%d) reached; dropping oldest trade",
                MAX_TRADES_PER_WALLET_WINDOW,
            )
            self._trades.popleft()
        if self._trades and trade_time < _as_utc(self._trades[-1].ledger_close_time):
            _record_late_event("out_of_order")
            index = len(self._trades)
            while index > 0 and _as_utc(self._trades[index - 1].ledger_close_time) > trade_time:
                index -= 1
            self._trades.insert(index, trade)
            return True
        self._trades.append(trade)
        return True

    def get(self, hours: int) -> List[Trade]:
        """Return trades whose ``ledger_close_time`` falls within the last *hours*."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [t for t in self._trades if _as_utc(t.ledger_close_time) >= cutoff]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict(self, hours: int) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        while self._trades and _as_utc(self._trades[0].ledger_close_time) < cutoff:
            self._trades.popleft()

    # ------------------------------------------------------------------
    # Serialisation helpers (for SQLite checkpoint)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "trades": [t.model_dump(mode="json") for t in self._trades],
            "last_score": self._last_score,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WalletWindow":
        ww = cls()
        for td in data.get("trades", []):
            # Reconstruct nested Asset objects
            td["base_asset"] = Asset(**td["base_asset"])
            td["counter_asset"] = Asset(**td["counter_asset"])
            ww._trades.append(Trade(**td))
        ww._last_score = data.get("last_score")
        return ww


def _as_utc(dt: datetime) -> datetime:
    """Return *dt* as UTC-aware, assuming UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RollingWindowState:
    """In-memory store of per-wallet :class:`WalletWindow` objects.

    Thread-safety: not thread-safe by design.  The streaming loop runs in a
    single thread; the graceful-shutdown handler calls
    :meth:`checkpoint_all` from a signal handler and must do so before the
    main loop exits.
    """

    def __init__(self) -> None:
        self._wallets: Dict[str, WalletWindow] = {}

    def add_trade(self, wallet: str, trade: Trade) -> None:
        """Add *trade* to *wallet*'s window, creating the window if absent."""
        if wallet not in self._wallets:
            self._wallets[wallet] = WalletWindow()
        self._wallets[wallet].add(trade)

    def get_window(self, wallet: str, hours: int) -> List[Trade]:
        """Return trades within the last *hours* for *wallet*."""
        if wallet not in self._wallets:
            return []
        return self._wallets[wallet].get(hours)

    def get_wallet_window(self, wallet: str) -> Optional[WalletWindow]:
        return self._wallets.get(wallet)

    @property
    def active_wallets(self) -> int:
        """Number of wallets with at least one trade in their 24-h window."""
        return len(self._wallets)

    def wallets(self) -> Dict[str, WalletWindow]:
        return self._wallets


class RollingWindowStore:
    """SQLite persistence for :class:`RollingWindowState`.

    Each wallet occupies one row in ``rolling_window_checkpoints``.  On
    :meth:`save_state` the full 24-h trade list is JSON-serialised via
    Pydantic's ``model_dump``; on :meth:`load_state` it is reconstructed
    without ``pickle``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with _connect(self._db_path) as conn:
            conn.executescript(_CHECKPOINT_SCHEMA)
            conn.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a raw connection to this store's database file.

        For callers (see :mod:`ingestion.stream_checkpoint`) that need to
        compose additional writes into the same atomic transaction as a
        window-state checkpoint — e.g. persisting a Horizon cursor alongside
        it so the two can never durably desync.
        """
        with _connect(self._db_path) as conn:
            yield conn

    def save_state(
        self, wallet: str, window: WalletWindow, conn: sqlite3.Connection | None = None
    ) -> None:
        """Upsert *window* for *wallet*.

        When *conn* is omitted, opens and commits its own connection (prior
        behavior, unchanged). When *conn* is supplied, writes on it without
        committing — the caller owns the transaction boundary.
        """
        data = json.dumps(window.to_dict())
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO rolling_window_checkpoints (wallet, trades_json, last_score, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                trades_json = excluded.trades_json,
                last_score  = excluded.last_score,
                updated_at  = excluded.updated_at
        """
        params = (wallet, data, window._last_score, now)
        if conn is not None:
            conn.execute(sql, params)
            return
        with _connect(self._db_path) as owned_conn:
            owned_conn.execute(sql, params)
            owned_conn.commit()

    def load_state(self, wallet: str) -> Optional[WalletWindow]:
        """Return the persisted :class:`WalletWindow` for *wallet*, or ``None``."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT trades_json, last_score FROM rolling_window_checkpoints WHERE wallet = ?",
                (wallet,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Corrupt checkpoint for wallet %s; ignoring", wallet)
            return None
        ww = WalletWindow.from_dict(data)
        ww._last_score = row[1]
        return ww

    def save_all(
        self, state: RollingWindowState, conn: sqlite3.Connection | None = None
    ) -> None:
        """Checkpoint every wallet in *state*.

        When *conn* is omitted, all wallets are written and committed as a
        single transaction on one connection (rather than one connection and
        commit per wallet), so a crash mid-checkpoint can never leave a
        partial mix of updated and stale wallet rows. When *conn* is
        supplied, all writes land on it without committing, so the caller can
        fold them into a larger atomic transaction (see
        :mod:`ingestion.stream_checkpoint`).
        """
        if conn is not None:
            for wallet, window in state.wallets().items():
                self.save_state(wallet, window, conn=conn)
            return
        with _connect(self._db_path) as owned_conn:
            for wallet, window in state.wallets().items():
                self.save_state(wallet, window, conn=owned_conn)
            owned_conn.commit()

    def load_all(self, state: RollingWindowState) -> None:
        """Populate *state* from all persisted checkpoints."""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT wallet, trades_json, last_score FROM rolling_window_checkpoints"
            ).fetchall()
        for wallet, trades_json, last_score in rows:
            try:
                data = json.loads(trades_json)
            except json.JSONDecodeError:
                logger.warning("Corrupt checkpoint for wallet %s; skipping", wallet)
                continue
            ww = WalletWindow.from_dict(data)
            ww._last_score = last_score
            state._wallets[wallet] = ww
        logger.info("Loaded %d wallet windows from checkpoint", len(rows))
