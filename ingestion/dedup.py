"""Unified, source-agnostic event deduplication and idempotency layer.

Overview
--------
LedgerLens ingests data from multiple sources: Stellar Horizon (trades),
EVM chains (bridge logs), and Solana (swap events). Each source can
deliver duplicate events due to network retries, restarts, concurrent backfills,
or block reorganizations.

This module provides:
1. `IdempotencyKeyStore`: A source-agnostic deduplicator that calculates stable,
   SHA-256 content hashes from key identity fields and stores them in the
   `ingestion_dedup_keys` SQLite table. It also maintains a chronological audit log
   in `ingestion_dedup_audit` for reporting deduplication stats via CLI.
2. `BridgeEventDeduplicator`: A backward-compatible thin wrapper around
   `IdempotencyKeyStore` that maps original EVM calls onto the new shared store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

from config.settings import settings

logger = logging.getLogger("ledgerlens.dedup")


class DedupResult(Enum):
    """Classification of an event by the deduplication layer."""

    NEW = "new"
    """Event has not been seen before and is within the replay protection window."""

    DUPLICATE = "duplicate"
    """Event hash already exists in the dedup table — skip and do not write."""

    REPLAY_REJECTED = "replay_rejected"
    """Event's timestamp is older than current_time minus replay_window_seconds, or too old."""


@dataclass
class DeduplicationStats:
    """Counters exposed by the deduplication layer."""

    seen_total: int
    duplicate_total: int
    replay_rejected_total: int
    duplicate_rate: float


def compute_event_hash(
    chain_id: int,
    tx_hash: str,
    log_index: int,
) -> str:
    """Stable, backward-compatible SHA-256 digest for an EVM event."""
    store = IdempotencyKeyStore()
    return store.compute_key("evm", chain_id=chain_id, tx_hash=tx_hash, log_index=log_index)


class IdempotencyKeyStore:
    """Source-agnostic content-hash dedup, generalizing BridgeEventDeduplicator.

    BridgeEventDeduplicator becomes a thin wrapper around this for backward
    compatibility; new callers use IdempotencyKeyStore directly.
    """

    def __init__(
        self,
        db_path: str | None = None,
        replay_window_seconds: float = 3600.0,
        db_conn: sqlite3.Connection | None = None,
    ) -> None:
        self.replay_window_seconds = replay_window_seconds
        self._lock = threading.Lock()

        # In-process counters
        self._seen_total: int = 0
        self._duplicate_total: int = 0
        self._replay_rejected_total: int = 0

        if db_conn is not None:
            self._conn = db_conn
            self._owns_conn = False
        else:
            self.db_path = db_path or settings.db_path
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            self._owns_conn = True

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the dedup and audit tables if they do not exist."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_dedup_keys (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source          TEXT NOT NULL,
                    metadata_json   TEXT,
                    first_seen_at   TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dedup_source 
                    ON ingestion_dedup_keys (source);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dedup_first_seen 
                    ON ingestion_dedup_keys (first_seen_at);
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_dedup_audit (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    result          TEXT NOT NULL,
                    checked_at      TEXT NOT NULL,
                    metadata_json   TEXT
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dedup_audit_source_time 
                    ON ingestion_dedup_audit (source, checked_at);
                """
            )

    def compute_key(self, source: str, **identity_fields: Any) -> str:
        """SHA-256 of `source` + sorted, normalised identity_fields."""
        normalized = {}
        for k, v in identity_fields.items():
            if isinstance(v, str):
                # Solana signature is case-sensitive base58, preserve case.
                if source == "solana" and k == "signature":
                    normalized[k] = v
                else:
                    normalized[k] = v.lower()
            elif isinstance(v, (int, float)):
                # Cast integer-like numbers to int
                normalized[k] = int(v) if v == int(v) else v
            else:
                normalized[k] = v

        payload = json.dumps(
            {
                "source": source,
                "identity_fields": normalized,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def is_duplicate(
        self,
        key: str,
        timestamp: datetime | float | None = None,
        source: str = "unknown",
        metadata: dict | None = None,
    ) -> DedupResult:
        """Classify a key as NEW, DUPLICATE, or REPLAY_REJECTED."""
        with self._lock:
            self._seen_total += 1

            # 1. Replay protection
            if timestamp is not None and self.replay_window_seconds > 0:
                if isinstance(timestamp, datetime):
                    if timestamp.tzinfo is not None:
                        event_time = timestamp.timestamp()
                    else:
                        event_time = timestamp.replace(tzinfo=timezone.utc).timestamp()
                elif isinstance(timestamp, str):
                    # parse ISO string to datetime
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    event_time = dt.timestamp()
                else:
                    event_time = float(timestamp)

                if time.time() - event_time > self.replay_window_seconds:
                    self._replay_rejected_total += 1
                    # Log replay rejection to audit table
                    now_str = datetime.now(timezone.utc).isoformat()
                    try:
                        with self._conn:
                            self._conn.execute(
                                """
                                INSERT INTO ingestion_dedup_audit 
                                    (idempotency_key, source, result, checked_at, metadata_json)
                                VALUES (?, ?, 'replay_rejected', ?, ?)
                                """,
                                (key, source, now_str, json.dumps(metadata) if metadata else None),
                            )
                    except Exception as e:
                        logger.warning("Failed to log replay rejection to audit table: %s", e)
                    return DedupResult.REPLAY_REJECTED

            # 2. Check duplicate
            row = self._conn.execute(
                "SELECT 1 FROM ingestion_dedup_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()

            if row is not None:
                self._duplicate_total += 1
                # Log duplicate to audit table
                now_str = datetime.now(timezone.utc).isoformat()
                try:
                    with self._conn:
                        self._conn.execute(
                            """
                            INSERT INTO ingestion_dedup_audit 
                                (idempotency_key, source, result, checked_at, metadata_json)
                            VALUES (?, ?, 'duplicate', ?, ?)
                            """,
                            (key, source, now_str, json.dumps(metadata) if metadata else None),
                        )
                except Exception as e:
                    logger.warning("Failed to log duplicate to audit table: %s", e)
                return DedupResult.DUPLICATE

            return DedupResult.NEW

    def mark_seen(self, key: str, source: str, metadata: dict | None = None) -> None:
        """Record a new event key in both keys and audit tables."""
        now_str = datetime.now(timezone.utc).isoformat()
        metadata_str = json.dumps(metadata) if metadata else None
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO ingestion_dedup_keys 
                        (idempotency_key, source, metadata_json, first_seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, source, metadata_str, now_str),
                )
                self._conn.execute(
                    """
                    INSERT INTO ingestion_dedup_audit 
                        (idempotency_key, source, result, checked_at, metadata_json)
                    VALUES (?, ?, 'new', ?, ?)
                    """,
                    (key, source, now_str, metadata_str),
                )

    def stats(self) -> DeduplicationStats:
        """Return snapshot of in-process deduplication counters."""
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

    def prune_old_entries(self, older_than_days: int = 90) -> int:
        """Delete keys and audits older than older_than_days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            with self._conn:
                cursor1 = self._conn.execute(
                    "DELETE FROM ingestion_dedup_keys WHERE first_seen_at < ?",
                    (cutoff,),
                )
                pruned_keys = cursor1.rowcount
                self._conn.execute(
                    "DELETE FROM ingestion_dedup_audit WHERE checked_at < ?",
                    (cutoff,),
                )
            logger.debug(
                "IdempotencyKeyStore: pruned %d keys older than %s",
                pruned_keys,
                cutoff,
            )
            return pruned_keys

    def close(self) -> None:
        if self._owns_conn and self._conn:
            self._conn.close()


class BridgeEventDeduplicator:
    """Backward-compatibility thin wrapper around IdempotencyKeyStore."""

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        replay_window_blocks: int = 1000,
    ) -> None:
        self._conn = db_conn
        self.replay_window_blocks = replay_window_blocks
        self._store = IdempotencyKeyStore(db_conn=db_conn)
        self._replay_rejected_total: int = 0

    def is_duplicate(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
        current_chain_head: int,
    ) -> DedupResult:
        # 1. Block-based replay protection
        if current_chain_head > 0:
            cutoff = current_chain_head - self.replay_window_blocks
            if block_number < cutoff:
                self._replay_rejected_total += 1
                return DedupResult.REPLAY_REJECTED

        key = self._store.compute_key("evm", chain_id=chain_id, tx_hash=tx_hash, log_index=log_index)
        metadata = {
            "chain_id": chain_id,
            "tx_hash": tx_hash,
            "log_index": log_index,
            "block_number": block_number,
        }
        # Forward check to shared store
        result = self._store.is_duplicate(key, source="evm", metadata=metadata)
        return result

    def mark_seen(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
    ) -> None:
        key = self._store.compute_key("evm", chain_id=chain_id, tx_hash=tx_hash, log_index=log_index)
        metadata = {
            "chain_id": chain_id,
            "tx_hash": tx_hash,
            "log_index": log_index,
            "block_number": block_number,
        }
        self._store.mark_seen(key, source="evm", metadata=metadata)

    def stats(self) -> DeduplicationStats:
        s = self._store.stats()
        # Merge block-based replay rejections into stats output
        total_rejected = s.replay_rejected_total + self._replay_rejected_total
        rate = (
            s.duplicate_total / s.seen_total
            if s.seen_total > 0
            else 0.0
        )
        return DeduplicationStats(
            seen_total=s.seen_total,
            duplicate_total=s.duplicate_total,
            replay_rejected_total=total_rejected,
            duplicate_rate=rate,
        )

    def prune_old_entries(
        self,
        current_chain_head: int,
        keep_blocks: int = 10_000,
    ) -> int:
        cutoff_block = current_chain_head - keep_blocks
        if cutoff_block <= 0:
            return 0

        with self._conn:
            # Query keys via json_extract in metadata
            cursor = self._conn.execute(
                """
                DELETE FROM ingestion_dedup_keys 
                WHERE source = 'evm' 
                  AND json_extract(metadata_json, '$.block_number') < ?
                """,
                (cutoff_block,),
            )
            pruned = cursor.rowcount
            self._conn.execute(
                """
                DELETE FROM ingestion_dedup_audit
                WHERE source = 'evm'
                  AND json_extract(metadata_json, '$.block_number') < ?
                """,
                (cutoff_block,),
            )
        logger.debug("BridgeEventDeduplicator: pruned %d entries", pruned)
        return pruned

    def handle_reorg(self, chain_id: int, reorg_from_block: int) -> int:
        with self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM ingestion_dedup_keys
                WHERE source = 'evm'
                  AND json_extract(metadata_json, '$.chain_id') = ?
                  AND json_extract(metadata_json, '$.block_number') >= ?
                """,
                (int(chain_id), int(reorg_from_block)),
            )
            invalidated = cursor.rowcount
            self._conn.execute(
                """
                DELETE FROM ingestion_dedup_audit
                WHERE source = 'evm'
                  AND json_extract(metadata_json, '$.chain_id') = ?
                  AND json_extract(metadata_json, '$.block_number') >= ?
                """,
                (int(chain_id), int(reorg_from_block)),
            )
        logger.info(
            "dedup: reorg on chain_id=%d from block %d — invalidated %d entries",
            chain_id,
            reorg_from_block,
            invalidated,
        )
        return invalidated
