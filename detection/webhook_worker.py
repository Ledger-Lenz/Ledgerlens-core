"""Async webhook delivery worker.

Polls the delivery queue, signs payloads with HMAC-SHA256, POSTs to
subscriber URLs, and marks results (delivered / failed / dead).
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from detection.webhook_queue import (
    get_dead_letters as _get_dead_letters,
    get_due_deliveries,
    mark_delivered,
    mark_failed,
    init_db as init_queue_db,
)
from detection.webhook_registry import get_subscriber, init_db as init_registry_db

logger = logging.getLogger("ledgerlens.webhook.worker")

MAX_CONCURRENT = 10
REQUEST_TIMEOUT = 10.0

get_dead_letters = _get_dead_letters


def build_webhook_payload(score_data: dict) -> dict:
    return {
        "event": "risk_score_alert",
        "data": score_data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_hmac_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _deliver(
    client: httpx.AsyncClient,
    delivery,
    subscriber,
    db_path: str | None = None,
) -> bool:
    score_data = json.loads(delivery.payload_json)
    payload = build_webhook_payload(score_data)
    body = json.dumps(payload).encode()
    signature = build_hmac_signature(body, subscriber.secret)

    headers = {
        "Content-Type": "application/json",
        "X-LedgerLens-Signature": signature,
        "X-LedgerLens-Timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    }

    try:
        resp = await client.post(
            subscriber.url,
            content=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        mark_delivered(delivery.id, resp.status_code, db_path=db_path)
        return True
    except httpx.HTTPStatusError as exc:
        error = f"HTTP {exc.response.status_code}"
        mark_failed(delivery.id, error, db_path=db_path)
        return False
    except Exception as exc:
        error = str(exc)[:200]
        mark_failed(delivery.id, error, db_path=db_path)
        return False


async def run_delivery_worker(
    interval_seconds: float = 5.0,
    db_path: str | None = None,
):
    init_registry_db(db_path)
    init_queue_db(db_path)
    logger.info(
        "Webhook delivery worker started (interval=%ss, max_concurrent=%d)",
        interval_seconds,
        MAX_CONCURRENT,
    )

    async with httpx.AsyncClient() as client:
        while True:
            try:
                deliveries = get_due_deliveries(db_path=db_path)
                if deliveries:
                    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

                    async def _deliver_one(d):
                        async with semaphore:
                            sub = get_subscriber(d.subscriber_id, db_path=db_path)
                            if sub and sub.active:
                                await _deliver(client, d, sub, db_path=db_path)

                    await asyncio.gather(*[_deliver_one(d) for d in deliveries])
            except Exception:
                logger.exception("Unhandled error in delivery worker loop")

            await asyncio.sleep(interval_seconds)
