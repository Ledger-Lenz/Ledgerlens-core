from __future__ import annotations
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from config.settings import settings

logger = logging.getLogger("ledgerlens.dlq")


class DLQErrorClass(str, Enum):
    PARSE_ERROR = "ParseError"
    NETWORK_ERROR = "NetworkError"
    SCHEMA_ERROR = "SchemaError"
    STORAGE_ERROR = "StorageError"
    VERSION_ERROR = "VersionError"
    UNKNOWN = "Unknown"


@dataclass
class DLQEntry:
    id: int | None
    source: str
    error_class: DLQErrorClass
    error_message: str
    raw_record: str       # JSON-serialised original record
    created_at: datetime
    retry_count: int
    status: str           # "pending", "replayed", "dead", "quarantined"
    replayed_at: datetime | None = None
    replay_failures: int = 0
    last_replay_error: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    error_class TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    replayed_at TEXT,
    replay_failures INTEGER NOT NULL DEFAULT 0,
    last_replay_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_dlq_status ON dead_letter_queue(status);
CREATE INDEX IF NOT EXISTS idx_trade_dlq_created ON dead_letter_queue(created_at);
"""

_COLUMNS = (
    "id, source, error_class, error_message, raw_record_json, created_at, "
    "retry_count, status, replayed_at, replay_failures, last_replay_error"
)

# Replay failures after which an entry is quarantined and never retried again.
DEFAULT_MAX_REPLAY_FAILURES = 3


@dataclass
class ReplayOutcome:
    entry_id: int
    status: str           # "replayed", "failed", "quarantined", "skipped"
    error: str | None = None


def _default_alert(entry: DLQEntry, error: str) -> None:
    logger.error(
        "DLQ_QUARANTINE entry_id=%s source=%s error_class=%s failures=%d last_error=%s",
        entry.id, entry.source, entry.error_class.value, entry.replay_failures, error,
    )


def _parse_entry(row: tuple) -> DLQEntry:
    (id_, source, error_class, error_message, raw_record_json, created_at,
     retry_count, status, replayed_at, replay_failures, last_replay_error) = row
    return DLQEntry(
        id=id_,
        source=source,
        error_class=DLQErrorClass(error_class),
        error_message=error_message,
        raw_record=raw_record_json,
        created_at=datetime.fromisoformat(created_at),
        retry_count=retry_count,
        status=status,
        replayed_at=datetime.fromisoformat(replayed_at) if replayed_at else None,
        replay_failures=replay_failures or 0,
        last_replay_error=last_replay_error,
    )


class TradeDLQ:
    """SQLite-backed Dead-Letter Queue for failed ingestion records."""

    def __init__(
        self,
        db_path: str | None = None,
        max_replay_failures: int = DEFAULT_MAX_REPLAY_FAILURES,
        alert_fn: Callable[[DLQEntry, str], None] | None = None,
    ) -> None:
        self._db_path = db_path or settings.db_path
        self._max_replay_failures = max_replay_failures
        self._alert_fn = alert_fn or _default_alert
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def push(self, source: str, error_class: DLQErrorClass, error_message: str, raw_record: Any) -> int:
        """Insert a failed record into the DLQ. Returns the new row id."""
        raw_json = json.dumps(raw_record) if not isinstance(raw_record, str) else raw_record
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO dead_letter_queue
                    (source, error_class, error_message, raw_record_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source, error_class.value, error_message, raw_json, now),
            )
            conn.commit()
            return cur.lastrowid

    def quarantine(
        self, source: str, error_class: DLQErrorClass, error_message: str, raw_record: Any
    ) -> int:
        """Insert a record directly as quarantined (never retried) and raise an alert."""
        row_id = self.push(source, error_class, error_message, raw_record)
        with self._connect() as conn:
            conn.execute(
                "UPDATE dead_letter_queue SET status = 'quarantined', last_replay_error = ? WHERE id = ?",
                (error_message, row_id),
            )
            conn.commit()
        self._on_quarantined(self.get(row_id), error_message)
        return row_id

    def list_entries(
        self,
        status: str | None = None,
        error_class: DLQErrorClass | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DLQEntry]:
        """List DLQ entries with optional filters."""
        conditions: list[str] = []
        params: list = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if error_class is not None:
            conditions.append("error_class = ?")
            params.append(error_class.value)
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS}
                FROM dead_letter_queue
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
        return [_parse_entry(row) for row in rows]

    def mark_replayed(self, entry_id: int) -> None:
        """Mark an entry as successfully replayed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE dead_letter_queue SET status = 'replayed', replayed_at = ?, retry_count = retry_count + 1 WHERE id = ?",
                (now, entry_id),
            )
            conn.commit()

    def mark_dead(self, entry_id: int) -> None:
        """Mark an entry as permanently dead (max retries exceeded)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE dead_letter_queue SET status = 'dead' WHERE id = ?",
                (entry_id,),
            )
            conn.commit()

    def get_replayable(
        self,
        error_class: DLQErrorClass | None = None,
        max_entries: int = 50,
    ) -> list[DLQEntry]:
        """Return pending entries eligible for replay.

        NetworkError entries are always replayable.
        Other classes require explicit operator action (pass error_class to override).
        """
        target_class = error_class if error_class is not None else DLQErrorClass.NETWORK_ERROR
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS}
                FROM dead_letter_queue
                WHERE status = 'pending' AND error_class = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (target_class.value, max_entries),
            ).fetchall()
        return [_parse_entry(row) for row in rows]

    def classify_exception(self, exc: Exception) -> DLQErrorClass:
        """Classify a Python exception into a DLQErrorClass."""
        exc_type = type(exc).__name__
        module = type(exc).__module__
        if "ValidationError" in exc_type or "pydantic" in module:
            return DLQErrorClass.PARSE_ERROR
        if "Timeout" in exc_type or "Connect" in exc_type or "Network" in exc_type:
            return DLQErrorClass.NETWORK_ERROR
        if "Schema" in exc_type or "HorizonSchema" in exc_type:
            return DLQErrorClass.SCHEMA_ERROR
        if "OperationalError" in exc_type or "sqlite3" in module:
            return DLQErrorClass.STORAGE_ERROR
        if "Version" in exc_type or "HorizonVersion" in exc_type:
            return DLQErrorClass.VERSION_ERROR
        return DLQErrorClass.UNKNOWN

    def get(self, entry_id: int) -> DLQEntry | None:
        """Return a single DLQ entry by id, or None if it does not exist."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM dead_letter_queue WHERE id = ?", (entry_id,)
            ).fetchone()
        return _parse_entry(row) if row else None

    def record_replay_failure(self, entry_id: int, error: str) -> bool:
        """Record a failed replay attempt; quarantine the entry once the limit is hit.

        Returns True when the entry was quarantined (and an alert was raised).
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE dead_letter_queue
                SET replay_failures = replay_failures + 1,
                    retry_count = retry_count + 1,
                    last_replay_error = ?,
                    status = CASE WHEN replay_failures + 1 >= ? THEN 'quarantined' ELSE status END
                WHERE id = ?
                """,
                (error, self._max_replay_failures, entry_id),
            )
            conn.commit()
        entry = self.get(entry_id)
        if entry is None or entry.status != "quarantined":
            return False
        self._on_quarantined(entry, error)
        return True

    def _on_quarantined(self, entry: DLQEntry | None, error: str) -> None:
        if entry is None:
            return
        from ingestion.metrics import get_metrics

        get_metrics().dlq_quarantined_total.labels(error_class=entry.error_class.value).inc()
        self._alert_fn(entry, error)

    def replay(self, entry_id: int, handler: Callable[[Any], Any]) -> ReplayOutcome:
        """Replay one entry through *handler* (called with the decoded record).

        Success marks the entry replayed; failure counts toward quarantine.
        Only pending entries are replayed.
        """
        entry = self.get(entry_id)
        if entry is None:
            return ReplayOutcome(entry_id, "skipped", "entry not found")
        if entry.status != "pending":
            return ReplayOutcome(entry_id, "skipped", f"entry status is {entry.status!r}")
        try:
            record = json.loads(entry.raw_record)
        except ValueError:
            record = entry.raw_record
        try:
            handler(record)
        except Exception as exc:  # noqa: BLE001 - any handler failure counts toward quarantine
            error = f"{type(exc).__name__}: {exc}"
            quarantined = self.record_replay_failure(entry_id, error)
            return ReplayOutcome(entry_id, "quarantined" if quarantined else "failed", error)
        self.mark_replayed(entry_id)
        return ReplayOutcome(entry_id, "replayed")

    def stats(self) -> dict[str, float | int]:
        """Return pending depth, quarantined count and age of the oldest pending entry."""
        with self._connect() as conn:
            depth, oldest = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM dead_letter_queue WHERE status = 'pending'"
            ).fetchone()
            quarantined = conn.execute(
                "SELECT COUNT(*) FROM dead_letter_queue WHERE status = 'quarantined'"
            ).fetchone()[0]
        age = 0.0
        if oldest:
            age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds())
        return {"depth": depth, "quarantined": quarantined, "oldest_age_seconds": age}

    def refresh_metrics(self) -> dict[str, float | int]:
        """Publish DLQ depth and oldest-entry age gauges; returns the stats."""
        stats = self.stats()
        from ingestion.metrics import get_metrics

        metrics = get_metrics()
        metrics.dlq_depth.set(stats["depth"])
        metrics.dlq_oldest_entry_age_seconds.set(stats["oldest_age_seconds"])
        return stats
