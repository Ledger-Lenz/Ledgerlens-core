"""Delivery queue for webhook alerts — SQLite-backed with at-least-once semantics.

Items stay ``pending`` until acknowledged.  Failed deliveries are retried with
exponential backoff (``2^N * 5s``, capped at 1 hour) minus up to 20% random
jitter so failing endpoints are not retried in lockstep.  After 8 attempts an
item moves to ``dead`` (dead-letter queue) and a ``webhook.dead_lettered``
ERROR log alert is emitted.
"""

import json
import logging
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger("ledgerlens.webhook.queue")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_delivery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_queue_status ON webhook_delivery_queue (status);
CREATE INDEX IF NOT EXISTS idx_webhook_queue_next_attempt ON webhook_delivery_queue (next_attempt_at);
"""

MAX_ATTEMPTS = 8
BASE_DELAY = 5          # seconds
MAX_DELAY = 3600        # 1 hour cap
JITTER_RATIO = 0.2      # up to 20% of the delay is randomly shaved off

_jitter = random.random


def compute_backoff(attempt: int) -> float:
    """Seconds to wait before retry *attempt*: capped exponential with jitter."""
    delay = min(2**attempt * BASE_DELAY, MAX_DELAY)
    return delay * (1 - JITTER_RATIO * _jitter())


@dataclass
class Delivery:
    id: int
    subscriber_id: str
    payload_json: str
    attempt_count: int
    next_attempt_at: str
    last_error: str | None
    status: str
    created_at: str
    delivered_at: str | None


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None):
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


# Initialize database at module import time (idempotent)
init_db()


def _row_to_delivery(row) -> Delivery:
    return Delivery(
        id=row[0],
        subscriber_id=row[1],
        payload_json=row[2],
        attempt_count=row[3],
        next_attempt_at=row[4],
        last_error=row[5],
        status=row[6],
        created_at=row[7],
        delivered_at=row[8],
    )


def enqueue(subscriber_id: str, payload: dict, db_path: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO webhook_delivery_queue (subscriber_id, payload_json, attempt_count, next_attempt_at, status, created_at) VALUES (?, ?, 0, ?, 'pending', ?)",
            (subscriber_id, payload_json, now, now),
        )
        conn.commit()


def get_due_deliveries(limit: int = 50, db_path: str | None = None) -> list[Delivery]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, subscriber_id, payload_json, attempt_count, next_attempt_at, last_error, status, created_at, delivered_at FROM webhook_delivery_queue WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [_row_to_delivery(r) for r in rows]


def mark_delivered(delivery_id: int, response_status: int, db_path: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET status = 'delivered', delivered_at = ?, last_error = ? WHERE id = ?",
            (now, f"HTTP {response_status}", delivery_id),
        )
        conn.commit()


def mark_failed(
    delivery_id: int, error: str, max_attempts: int = MAX_ATTEMPTS, db_path: str | None = None
) -> str | None:
    """Record a failed attempt; return the new status (``pending``/``dead``)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count FROM webhook_delivery_queue WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        if not row:
            return None

        attempt = row[0] + 1
        now = datetime.now(timezone.utc)

        if attempt >= max_attempts:
            conn.execute(
                "UPDATE webhook_delivery_queue SET attempt_count = ?, status = 'dead', last_error = ? WHERE id = ?",
                (attempt, error, delivery_id),
            )
            status = "dead"
            logger.error(
                "webhook.dead_lettered delivery_id=%d attempts=%d last_error=%s",
                delivery_id,
                attempt,
                error,
            )
        else:
            next_at = (now + timedelta(seconds=compute_backoff(attempt))).isoformat()
            conn.execute(
                "UPDATE webhook_delivery_queue SET attempt_count = ?, next_attempt_at = ?, last_error = ?, status = 'pending' WHERE id = ?",
                (attempt, next_at, error, delivery_id),
            )
            status = "pending"
        conn.commit()
        return status


def get_dead_letters(db_path: str | None = None) -> list[Delivery]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, subscriber_id, payload_json, attempt_count, next_attempt_at, last_error, status, created_at, delivered_at FROM webhook_delivery_queue WHERE status = 'dead' ORDER BY created_at"
        ).fetchall()
        return [_row_to_delivery(r) for r in rows]
