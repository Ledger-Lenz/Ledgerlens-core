"""Cross-chain bridge event deduplication layer.

Overview
--------
EVM bridge events (Allbridge TokensSent, Uniswap Swap) arrive via JSON-RPC
``eth_getLogs`` calls and can be delivered more than once for legitimate reasons:

* **RPC retries** — a timed-out ``getLogs`` call is retried against an overlapping
  block range, so the same event appears in multiple responses.
* **Block reorganisations** — a log included in a block that is later reorged may
  be re-emitted when the canonical chain adopts a replacement block.  The event
  fields are identical, but ``blockHash`` and sometimes ``blockNumber`` change.
* **Restart replay** — each loader re-scans a small lookback window on startup to
  catch events that arrived during downtime; these overlap with previously
  processed events.

Without deduplication, duplicate events propagate into the feature engineering
pipeline, artificially inflating cross-chain volume metrics and generating false
wash-trading alerts.

Deduplication strategy
----------------------
Each event is identified by a **content hash** — a stable SHA-256 digest
computed from the event's *immutable* fields: ``chain_id``, ``tx_hash``, and
``log_index``.  These fields uniquely identify a log entry on the canonical
chain and do **not** include ``block_hash`` or ``block_number``, which change
across reorgs.

The hash is stored in a ``bridge_event_dedup`` SQLite table.  Before writing an
event to the main ``bridge_transfers`` table, callers:

1. Call :meth:`BridgeEventDeduplicator.is_duplicate` to classify the event as
   ``NEW``, ``DUPLICATE``, or ``REPLAY_REJECTED``.
2. Call :meth:`BridgeEventDeduplicator.mark_seen` for ``NEW`` events, then write
   to the main table.
3. Skip ``DUPLICATE`` and ``REPLAY_REJECTED`` events with appropriate log output.

Replay protection
-----------------
An adversary could submit a bridge event from thousands of blocks ago as if it
were recent, artificially inflating cross-chain volume for a wallet under
investigation.  :attr:`BridgeEventDeduplicator.replay_window_blocks` rejects
events whose ``block_number < current_chain_head - replay_window_blocks``.

Reorg handling
--------------
When the loader detects a block reorganisation at height *H*, it calls
:meth:`BridgeEventDeduplicator.handle_reorg` to delete all dedup entries for
``block_number >= H`` on the affected chain.  The corresponding events are then
reprocessed from the canonical chain, producing fresh dedup records.

Security notes
--------------
* Content hashes are computed from **normalised** inputs: ``tx_hash`` is
  lowercased and ``log_index`` is cast to ``int``.  This prevents hash-bypass
  attacks that exploit case differences or type coercions in attacker-controlled
  RPC responses.
* All SQL queries use parameterised statements — never f-string or format-string
  SQL — to prevent SQL injection from attacker-controlled ``tx_hash`` values.
* Dedup table writes are wrapped in transactions to prevent partial writes on
  process kill.
* The ``first_seen_at`` column is an audit trail: rows are *only* deleted by
  :meth:`prune_old_entries` (scheduled maintenance) or :meth:`handle_reorg`
  (explicit reorg recovery) — never by normal dedup logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ledgerlens.dedup")

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bridge_event_dedup (
    event_hash   TEXT    PRIMARY KEY,
    chain_id     INTEGER NOT NULL,
    tx_hash      TEXT    NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dedup_chain_block
    ON bridge_event_dedup (chain_id, block_number);
"""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class DedupResult(Enum):
    """Classification of a bridge event by the deduplication layer."""

    NEW = "new"
    """Event has not been seen before and is within the replay protection window."""

    DUPLICATE = "duplicate"
    """Event hash already exists in the dedup table — skip and do not write."""

    REPLAY_REJECTED = "replay_rejected"
    """Event's block_number is older than current_chain_head minus replay_window_blocks."""


@dataclass
class DeduplicationStats:
    """Counters exposed by :class:`BridgeEventDeduplicator`.

    These are in-process counters that reset when the deduplicator is
    recreated.  They are suitable for Prometheus metrics and structured
    log output.

    Attributes
    ----------
    seen_total:
        Total number of events passed to :meth:`BridgeEventDeduplicator.is_duplicate`.
    duplicate_total:
        Number of events classified as :attr:`DedupResult.DUPLICATE`.
    replay_rejected_total:
        Number of events classified as :attr:`DedupResult.REPLAY_REJECTED`.
    duplicate_rate:
        ``duplicate_total / seen_total`` when ``seen_total > 0``, else ``0.0``.
    """

    seen_total: int
    duplicate_total: int
    replay_rejected_total: int
    duplicate_rate: float


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def compute_event_hash(
    chain_id: int,
    tx_hash: str,
    log_index: int,
) -> str:
    """Return a stable, reorg-resistant identifier for a bridge event.

    The hash is computed from the event's *immutable* fields only.
    ``block_hash`` and ``block_number`` are deliberately excluded because
    they change across block reorganisations.

    Inputs are normalised before hashing:
    * ``tx_hash`` is lowercased — prevents bypass via mixed-case hex strings.
    * ``log_index`` is cast to ``int`` — prevents bypass via string coercions.

    Parameters
    ----------
    chain_id:
        EVM chain ID (e.g. 1 for Ethereum mainnet, 8453 for Base).
    tx_hash:
        Transaction hash hex string (case-insensitive; normalised internally).
    log_index:
        Zero-based index of this log entry within its transaction receipt.

    Returns
    -------
    str
        Lowercase hex-encoded SHA-256 digest (64 characters).

    Examples
    --------
    >>> compute_event_hash(1, "0xABCD", 0) == compute_event_hash(1, "0xabcd", 0)
    True
    >>> compute_event_hash(1, "0xabcd", 0) != compute_event_hash(1, "0xabcd", 1)
    True
    """
    payload = json.dumps(
        {
            "chain_id": int(chain_id),
            "tx_hash": tx_hash.lower(),
            "log_index": int(log_index),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------


class BridgeEventDeduplicator:
    """Idempotent deduplication layer for EVM bridge events.

    Stores a content-hash index in a ``bridge_event_dedup`` SQLite table and
    classifies incoming events as new, duplicate, or replay-rejected before they
    are written to the main ingestion tables.

    Parameters
    ----------
    db_conn:
        An open :class:`sqlite3.Connection`.  The deduplicator does **not** take
        ownership of the connection and will not close it.
    replay_window_blocks:
        Events whose ``block_number < current_chain_head - replay_window_blocks``
        are classified as :attr:`DedupResult.REPLAY_REJECTED`.  Defaults to
        ``1000`` blocks (≈ 3–4 hours on Ethereum, ≈ 30 minutes on Polygon).

    Thread-safety
    -------------
    The deduplicator is **not** thread-safe.  Each thread / async task should
    create its own instance backed by a separate SQLite connection, or
    serialise access with a lock.
    """

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        replay_window_blocks: int = 1000,
    ) -> None:
        self._conn = db_conn
        self.replay_window_blocks = replay_window_blocks

        # In-process counters
        self._seen_total: int = 0
        self._duplicate_total: int = 0
        self._replay_rejected_total: int = 0

        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the dedup table and index if they do not already exist."""
        with self._conn:
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.execute(_CREATE_INDEX_SQL)

    # ------------------------------------------------------------------
    # Core dedup API
    # ------------------------------------------------------------------

    def is_duplicate(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
        current_chain_head: int,
    ) -> DedupResult:
        """Classify an event as NEW, DUPLICATE, or REPLAY_REJECTED.

        This method **increments internal counters** but does **not** write to
        the dedup table.  Call :meth:`mark_seen` for ``NEW`` events after
        writing the event to the main table.

        Parameters
        ----------
        chain_id:
            EVM chain identifier.
        tx_hash:
            Transaction hash of the event.
        log_index:
            Log index within the transaction receipt.
        block_number:
            Block in which the event was mined.
        current_chain_head:
            The latest known block number for this chain, used to compute the
            replay protection cutoff.

        Returns
        -------
        DedupResult
            :attr:`DedupResult.REPLAY_REJECTED` when the event is too old.
            :attr:`DedupResult.DUPLICATE` when the hash already exists.
            :attr:`DedupResult.NEW` otherwise.
        """
        self._seen_total += 1

        # Replay protection: reject events older than the rolling window.
        # current_chain_head == 0 means the head is unknown; skip the check.
        if current_chain_head > 0:
            cutoff = current_chain_head - self.replay_window_blocks
            if block_number < cutoff:
                self._replay_rejected_total += 1
                return DedupResult.REPLAY_REJECTED

        event_hash = compute_event_hash(chain_id, tx_hash, log_index)
        row = self._conn.execute(
            "SELECT 1 FROM bridge_event_dedup WHERE event_hash = ?",
            (event_hash,),
        ).fetchone()

        if row is not None:
            self._duplicate_total += 1
            return DedupResult.DUPLICATE

        return DedupResult.NEW

    def mark_seen(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
    ) -> None:
        """Record a new event in the dedup table.

        Must be called only for events classified as :attr:`DedupResult.NEW`
        by :meth:`is_duplicate`.  The write is wrapped in a transaction so a
        process kill mid-write does not leave a partial row.

        Parameters
        ----------
        chain_id:
            EVM chain identifier.
        tx_hash:
            Transaction hash of the event.
        log_index:
            Log index within the transaction receipt.
        block_number:
            Block in which the event was mined.
        """
        event_hash = compute_event_hash(chain_id, tx_hash, log_index)
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO bridge_event_dedup
                    (event_hash, chain_id, tx_hash, log_index, block_number)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_hash, int(chain_id), tx_hash.lower(), int(log_index), int(block_number)),
            )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> DeduplicationStats:
        """Return a snapshot of the in-process deduplication counters.

        Returns
        -------
        DeduplicationStats
            A frozen snapshot of current counters.  The ``duplicate_rate``
            field is ``0.0`` when no events have been seen yet.
        """
        rate = (
            self._duplicate_total / self._seen_total
            if self._seen_total > 0
            else 0.0
        )
        return DeduplicationStats(
            seen_total=self._seen_total,
            duplicate_total=self._duplicate_total,
            replay_rejected_total=self._replay_rejected_total,
            duplicate_rate=rate,
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_old_entries(
        self,
        current_chain_head: int,
        keep_blocks: int = 10_000,
    ) -> int:
        """Delete dedup entries for blocks older than the keep window.

        This prevents unbounded table growth.  Should be called periodically
        (e.g. once per day or every N ingestion cycles).

        Parameters
        ----------
        current_chain_head:
            Latest known block number for the chain being pruned.
        keep_blocks:
            Retain entries for blocks within ``current_chain_head - keep_blocks``.
            Defaults to ``10_000`` blocks (≈ 33 hours on Ethereum at ~12s/block).

        Returns
        -------
        int
            Number of rows deleted.
        """
        cutoff_block = current_chain_head - keep_blocks
        if cutoff_block <= 0:
            return 0

        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM bridge_event_dedup WHERE block_number < ?",
                (cutoff_block,),
            )
        pruned = cursor.rowcount
        logger.debug(
            "dedup: pruned %d entries with block_number < %d (head=%d, keep=%d)",
            pruned,
            cutoff_block,
            current_chain_head,
            keep_blocks,
        )
        return pruned

    # ------------------------------------------------------------------
    # Reorg handling
    # ------------------------------------------------------------------

    def handle_reorg(self, chain_id: int, reorg_from_block: int) -> int:
        """Invalidate dedup entries affected by a block reorganisation.

        When the loader detects that block *H* has a different hash than
        recorded, all dedup entries for ``chain_id`` with
        ``block_number >= reorg_from_block`` are deleted.  The affected events
        should then be reprocessed from the canonical chain, which will produce
        fresh dedup records via :meth:`mark_seen`.

        Parameters
        ----------
        chain_id:
            The chain affected by the reorg.
        reorg_from_block:
            The first block whose canonical hash differs from what was recorded.
            All entries at this height and above are invalidated.

        Returns
        -------
        int
            Number of dedup rows deleted (i.e. the number of events that will
            be reprocessed).
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM bridge_event_dedup
                WHERE chain_id = ? AND block_number >= ?
                """,
                (int(chain_id), int(reorg_from_block)),
            )
        invalidated = cursor.rowcount
        logger.info(
            "dedup: reorg on chain_id=%d from block %d — invalidated %d entries",
            chain_id,
            reorg_from_block,
            invalidated,
        )
        return invalidated
