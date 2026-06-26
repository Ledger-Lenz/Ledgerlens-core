"""WebhookRetryQueue — asyncio-based retry scheduler with dead-letter storage.

Retry schedule: 30 s → 5 min → 30 min (3 attempts total).
After all retries are exhausted the delivery is written to the
``webhook_dlq`` SQLite table and a ``webhook.dead_lettered`` log event
is emitted.  HMAC-SHA256 signatures are re-computed on every attempt
using the same ``X-LedgerLens-Signature`` header scheme as the primary
delivery worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from config.settings import settings

logger = logging.getLogger("ledgerlens.webhook.sender")

RETRY_DELAYS = [30, 300, 1800]  # 30 s, 5 min, 30 min
REQUEST_TIMEOUT = 10.0

_DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_dlq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id TEXT NOT NULL,
    url TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    dead_lettered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_dlq_subscriber ON webhook_dlq (subscriber_id);
CREATE INDEX IF NOT EXISTS idx_webhook_dlq_dead_lettered ON webhook_dlq (dead_lettered_at);
"""


@dataclass
class DLQEntry:
    id: int
    subscriber_id: str
    url: str
    payload_json: dict
    attempt_count: int
    last_error: str | None
    dead_lettered_at: str


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_dlq(db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_DLQ_SCHEMA)
        conn.commit()


def _write_dlq(
    subscriber_id: str,
    url: str,
    payload: dict,
    attempt_count: int,
    last_error: str | None,
    db_path: str | None = None,
) -> int:
    init_dlq(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO webhook_dlq (subscriber_id, url, payload_json, attempt_count, last_error, dead_lettered_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (subscriber_id, url, json.dumps(payload), attempt_count, last_error, now),
        )
        conn.commit()
        return cur.lastrowid


def list_dlq(db_path: str | None = None) -> list[DLQEntry]:
    init_dlq(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, subscriber_id, url, payload_json, attempt_count, last_error, dead_lettered_at"
            " FROM webhook_dlq ORDER BY dead_lettered_at DESC"
        ).fetchall()
    return [
        DLQEntry(
            id=r["id"],
            subscriber_id=r["subscriber_id"],
            url=r["url"],
            payload_json=json.loads(r["payload_json"]),
            attempt_count=r["attempt_count"],
            last_error=r["last_error"],
            dead_lettered_at=r["dead_lettered_at"],
        )
        for r in rows
    ]


def get_dlq_entry(entry_id: int, db_path: str | None = None) -> DLQEntry | None:
    init_dlq(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, subscriber_id, url, payload_json, attempt_count, last_error, dead_lettered_at"
            " FROM webhook_dlq WHERE id = ?",
            (entry_id,),
        ).fetchone()
    if not row:
        return None
    return DLQEntry(
        id=row["id"],
        subscriber_id=row["subscriber_id"],
        url=row["url"],
        payload_json=json.loads(row["payload_json"]),
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        dead_lettered_at=row["dead_lettered_at"],
    )


def delete_dlq_entry(entry_id: int, db_path: str | None = None) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM webhook_dlq WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0


def _build_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_headers(body: bytes, secret: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-LedgerLens-Signature": _build_signature(body, secret),
        "X-LedgerLens-Timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    }


async def _attempt_delivery(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    secret: str,
) -> None:
    body = json.dumps(payload).encode()
    headers = _build_headers(body, secret)
    resp = await client.post(url, content=body, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()


class WebhookRetryQueue:
    """Asyncio-based retry queue with 3-attempt exponential schedule and DLQ."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        init_dlq(db_path)

    def schedule(
        self,
        subscriber_id: str,
        url: str,
        secret: str,
        payload: dict,
    ) -> None:
        asyncio.ensure_future(
            self._run_with_retries(subscriber_id, url, secret, payload)
        )

    async def _run_with_retries(
        self,
        subscriber_id: str,
        url: str,
        secret: str,
        payload: dict,
    ) -> None:
        last_error: str | None = None
        attempt = 0

        async with httpx.AsyncClient() as client:
            for delay in RETRY_DELAYS:
                await asyncio.sleep(delay)
                attempt += 1
                try:
                    await _attempt_delivery(client, url, payload, secret)
                    logger.info(
                        "webhook.retry_delivered subscriber=%s attempt=%d",
                        subscriber_id,
                        attempt,
                    )
                    return
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP {exc.response.status_code}"
                    logger.warning(
                        "webhook.retry_failed subscriber=%s attempt=%d error=%s",
                        subscriber_id,
                        attempt,
                        last_error,
                    )
                except Exception as exc:
                    last_error = str(exc)[:200]
                    logger.warning(
                        "webhook.retry_failed subscriber=%s attempt=%d error=%s",
                        subscriber_id,
                        attempt,
                        last_error,
                    )

        entry_id = _write_dlq(
            subscriber_id=subscriber_id,
            url=url,
            payload=payload,
            attempt_count=attempt,
            last_error=last_error,
            db_path=self._db_path,
        )
        logger.error(
            "webhook.dead_lettered subscriber=%s dlq_id=%d last_error=%s",
            subscriber_id,
            entry_id,
            last_error,
        )

    async def retry_dlq_entry(
        self,
        entry_id: int,
        secret: str,
    ) -> bool:
        entry = get_dlq_entry(entry_id, db_path=self._db_path)
        if not entry:
            return False

        async with httpx.AsyncClient() as client:
            try:
                await _attempt_delivery(client, entry.url, entry.payload_json, secret)
                delete_dlq_entry(entry_id, db_path=self._db_path)
                logger.info(
                    "webhook.dlq_retry_delivered dlq_id=%d subscriber=%s",
                    entry_id,
                    entry.subscriber_id,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "webhook.dlq_retry_failed dlq_id=%d error=%s",
                    entry_id,
                    str(exc)[:200],
                )
                return False
