from __future__ import annotations
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

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
    id: Optional[int]
    source: str
    error_class: DLQErrorClass
    error_message: str
    raw_record: str       # JSON-serialised original record
    created_at: datetime
    retry_count: int
    status: str           # "pending", "replayed", "dead"
    replayed_at: Optional[datetime] = None


def _parse_entry(row: tuple) -> DLQEntry:
    id_, source, error_class, error_message, raw_record_json, created_at, retry_count, status, replayed_at = row
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
    )


class TradeDLQ:
    """SQLite-backed Dead-Letter Queue for failed ingestion records."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path

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
                SELECT id, source, error_class, error_message, raw_record_json,
                       created_at, retry_count, status, replayed_at
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
                """
                SELECT id, source, error_class, error_message, raw_record_json,
                       created_at, retry_count, status, replayed_at
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
