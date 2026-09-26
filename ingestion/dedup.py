"""Unified, source-agnostic event deduplication and idempotency layer.

Overview
--------
LedgerLens ingests data from multiple sources: Stellar Horizon (trades),
EVM chains (bridge logs), and Solana (swap events). Each source can
deliver duplicate events due to network retries, restarts, concurrent backfills,
or block reorganizations.

This module provides:
1. `IdempotencyKeyStore`: A source-agnostic deduplicator that calculates stable,
   SHA-256 content hashes from key identity fields and stores them in a
   shared, TTL-bounded distributed store (Redis when configured, otherwise a
   bounded in-process fallback). It also maintains a chronological audit log
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

# Default TTL for dedup keys. Sized to comfortably exceed the realistic
# out-of-order / replay bounds used by ingestion/replay_buffer.py so that a
# key is never evicted while a legitimate replay could still arrive.
DEFAULT_DEDUP_TTL_SECONDS = 86400.0  # 24h

# Redis key namespace for the distributed dedup store.
DEDUP_REDIS_PREFIX = "ledgerlens:dedup:"


def _resolve_dedup_ttl(replay_window_seconds: float) -> float:
    """Size the dedup TTL from the replay window.

    The replay buffer tolerates out-of-order events up to its configured
    window; the dedup store must retain keys at least that long (with a
    safety multiplier) so a replayed event is still recognised as a
    duplicate rather than being treated as new.
    """
    try:
        from ingestion.replay_buffer import ReplayBuffer  # noqa: F401
    except Exception:  # pragma: no cover - optional import
        pass
    base = replay_window_seconds if replay_window_seconds and replay_window_seconds > 0 else 3600.0
    return max(base * 2.0, DEFAULT_DEDUP_TTL_SECONDS)


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
    return IdempotencyKeyStore.compute_key_static(
        "evm", chain_id=chain_id, tx_hash=tx_hash, log_index=log_index
    )


class _InMemoryDedupStore:
    """Bounded, TTL'd in-process fallback dedup store.

    Used when no shared Redis backend is configured (e.g. single-instance
    deployments or tests). Keys expire after `ttl_seconds` and the store is
    capped at `max_keys` to keep memory bounded under sustained load.
    """

    def __init__(self, ttl_seconds: float, max_keys: int = 1_000_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, exp in self._expiry.items() if exp <= now]
        for k in expired:
            self._expiry.pop(k, None)

    def add_if_absent(self, key: str) -> bool:
        """Return True if the key was newly added, False if already present."""
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            if key in self._expiry:
                return False
            if len(self._expiry) >= self.max_keys:
                # Drop the soonest-to-expire key to stay bounded.
                oldest = min(self._expiry, key=self._expiry.get)
                self._expiry.pop(oldest, None)
            self._expiry[key] = now + self.ttl_seconds
            return True

    def __len__(self) -> int:
        with self._lock:
            self._evict_expired(time.time())
            return len(self._expiry)


class IdempotencyKeyStore:
    """Source-agnostic content-hash dedup, generalizing BridgeEventDeduplicator.

    BridgeEventDeduplicator becomes a thin wrapper around this for backward
    compatibility; new callers use IdempotencyKeyStore directly.

    Dedup state lives in a shared, TTL-bounded distributed store (Redis) when
    available so that horizontally-scaled ingestion instances agree on what
    has already been processed. When Redis is not configured, a bounded
    in-process store is used as a fallback.
    """

    def __init__(
        self,
        db_path: str | None = None,
        replay_window_seconds: float = 3600.0,
        db_conn: sqlite3.Connection | None = None,
        redis_client: Any | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self.replay_window_seconds = replay_window_seconds
        self.ttl_seconds = ttl_seconds or _resolve_dedup_ttl(replay_window_seconds)
        self._lock = threading.Lock()

        # In-process counters
        self._seen_total: int = 0
        self._duplicate_total: int = 0
        self._replay_rejected_total: int = 0

        # Distributed dedup store (Redis) with bounded in-process fallback.
        self._redis = redis_client if redis_client is not None else self._connect_redis()
        self._fallback = _InMemoryDedupStore(self.ttl_seconds)

        if db_conn is not None:
            self._conn = db_conn
            self._owns_conn = False
        else:
            self.db_path = db_path or settings.db_path
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            self._owns_conn = True

        self._ensure_schema()

    @staticmethod
    def _connect_redis() -> Any | None:
        """Best-effort connection to a shared Redis dedup store."""
        url = getattr(settings, "redis_url", None)
        if not url:
            return None
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            return client
        except Exception as exc:  # pragma: no cover - optional dependency
            logger.warning("Redis dedup store unavailable, using in-process fallback: %s", exc)
            return None

    def _store_add_if_absent(self, key: str) -> bool:
        """Atomically register `key` in the shared store; True if newly added.

        Uses Redis SET NX with a TTL so keys are bounded and shared across
        instances. Falls back to the bounded in-process store otherwise.
        """
        if self._redis is not None:
            try:
                redis_key = f"{DEDUP_REDIS_PREFIX}{key}"
                added = self._redis.set(redis_key, "1", nx=True, ex=int(self.ttl_seconds))
                return bool(added)
            except Exception as exc:  # pragma: no cover - runtime resilience
                logger.warning("Redis dedup check failed, using fallback: %s", exc)
        return self._fallback.add_if_absent(key)

    def dedup_store_size(self) -> int:
        """Approximate number of live dedup keys (for memory/key-growth metrics)."""
        if self._redis is not None:
            try:
                return int(self._redis.dbsize())
            except Exception:  # pragma: no cover
                pass
        return len(self._fallback)

    def dedup_hit_rate(self) -> float:
        """Fraction of seen events classified as duplicates (observable metric)."""
        if self._seen_total == 0:
            return 0.0
        return self._duplicate_total / self._seen_total

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

    @staticmethod
    def compute_key_static(source: str, **identity_fields: Any) -> str:
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

    def compute_key(self, source: str, **identity_fields: Any) -> str:
        """SHA-256 of `source` + sorted, normalised identity_fields."""
        return self.compute_key_static(source, **identity_fields)

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

            # 2. Distributed, TTL-bounded dedup check.
            added = self._store_add_if_absent(key)
            if not added:
                self._duplicate_total += 1
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

            # 3. New event — record it in the audit log.
            now_str = datetime.now(timezone.utc).isoformat()
            try:
                with self._conn:
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO ingestion_dedup_keys 
                            (idempotency_key, source, metadata_json, first_seen_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, source, json.dumps(metadata) if metadata else None, now_str),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO ingestion_dedup_audit 
                            (idempotency_key, source, result, checked_at, metadata_json)
                        VALUES (?, ?, 'new', ?, ?)
                        """,
                        (key, source, now_str, json.dumps(metadata) if metadata else None),
                    )
            except Exception as e:
                logger.warning("Failed to record new dedup key: %s", e)
            return DedupResult.NEW

    def stats(self) -> DeduplicationStats:
        """Return current deduplication counters."""
        with self._lock:
            rate = self._duplicate_total / self._seen_total if self._seen_total else 0.0
            return DeduplicationStats(
                seen_total=self._seen_total,
                duplicate_total=self._duplicate_total,
                replay_rejected_total=self._replay_rejected_total,
                duplicate_rate=rate,
            )

    def prune_expired(self, older_than_seconds: float | None = None) -> int:
        """Delete dedup keys older than the TTL window; returns rows removed.

        Keeps the SQLite audit/key tables bounded in lockstep with the
        distributed store's TTL.
        """
        window = older_than_seconds if older_than_seconds is not None else self.ttl_seconds
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM ingestion_dedup_keys WHERE first_seen_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0


class BridgeEventDeduplicator:
    """Backward-compatible wrapper around IdempotencyKeyStore for EVM events."""

    def __init__(
        self,
        db_path: str | None = None,
        replay_window_seconds: float = 3600.0,
        db_conn: sqlite3.Connection | None = None,
        redis_client: Any | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self._store = IdempotencyKeyStore(
            db_path=db_path,
            replay_window_seconds=replay_window_seconds,
            db_conn=db_conn,
            redis_client=redis_client,
            ttl_seconds=ttl_seconds,
        )

    def compute_event_hash(self, chain_id: int, tx_hash: str, log_index: int) -> str:
        return compute_event_hash(chain_id, tx_hash, log_index)

    def is_duplicate(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        timestamp: datetime | float | None = None,
        metadata: dict | None = None,
    ) -> DedupResult:
        key = compute_event_hash(chain_id, tx_hash, log_index)
        return self._store.is_duplicate(
            key, timestamp=timestamp, source="evm", metadata=metadata
        )

    def stats(self) -> DeduplicationStats:
        return self._store.stats()
