"""Tests for ``detection.webhook_worker``."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from detection.risk_score import RiskScore


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "worker.db")


@pytest.fixture(autouse=True)
def _fix_settings(monkeypatch, db_path):
    """Make sure ``settings.db_path`` points at the temp DB so that
    ``mark_delivered`` / ``mark_failed`` (called without ``db_path=``)
    write to the right place."""
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    import config.settings as s

    object.__setattr__(s.settings, "db_path", db_path)


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------


def test_build_hmac_signature_is_verifiable():
    from detection.webhook_worker import build_hmac_signature

    body = b'{"event": "risk_score_alert", "data": {"wallet": "GABC"}}'
    secret = "whsec_test_secret"
    sig = build_hmac_signature(body, secret)

    assert sig.startswith("sha256=")
    hex_part = sig[len("sha256="):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert hex_part == expected


def test_hmac_signature_different_body_different_signature():
    from detection.webhook_worker import build_hmac_signature

    sig1 = build_hmac_signature(b'{"a": 1}', "secret")
    sig2 = build_hmac_signature(b'{"a": 2}', "secret")
    assert sig1 != sig2


def test_hmac_signature_different_secret_different_signature():
    from detection.webhook_worker import build_hmac_signature

    sig1 = build_hmac_signature(b'{"a": 1}', "secret1")
    sig2 = build_hmac_signature(b'{"a": 1}', "secret2")
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# Webhook payload builder
# ---------------------------------------------------------------------------


def test_build_webhook_payload():
    from detection.webhook_worker import build_webhook_payload

    score_data = {"wallet": "GABC", "score": 85}
    payload = build_webhook_payload(score_data)

    assert payload["event"] == "risk_score_alert"
    assert payload["data"] == score_data
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# _deliver — successful
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_success_marks_delivered(db_path):
    from detection.webhook_queue import enqueue, get_dead_letters, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    sub_id = register_subscriber("https://example.com/webhook", "whsec_secret", db_path=db_path)
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is True
    assert len(get_due_deliveries(db_path=db_path)) == 0
    assert len(get_dead_letters(db_path=db_path)) == 0


# ---------------------------------------------------------------------------
# _deliver — HTTP error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_http_500_triggers_retry(db_path):
    from detection.webhook_queue import enqueue, get_dead_letters, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    sub_id = register_subscriber("https://example.com/webhook", "whsec_secret", db_path=db_path)
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is False

    # Check the delivery is still pending with incremented attempt
    from detection.webhook_queue import _connect

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, status, last_error FROM webhook_delivery_queue WHERE id = ?",
            (deliveries[0].id,),
        ).fetchone()
    assert row[0] == 1  # attempt_count
    assert row[1] == "pending"
    assert row[2] == "HTTP 500"


# ---------------------------------------------------------------------------
# _deliver — moves to dead after max attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_moves_to_dead_after_8_attempts(db_path):
    from detection.webhook_queue import _connect, enqueue, get_dead_letters, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    sub_id = register_subscriber("https://example.com/webhook", "whsec_secret", db_path=db_path)
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET attempt_count = 7 WHERE id = ?",
            (deliveries[0].id,),
        )
        conn.commit()

    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is False

    dead = get_dead_letters(db_path=db_path)
    assert len(dead) == 1
    assert dead[0].status == "dead"
    assert dead[0].attempt_count == 8


# ---------------------------------------------------------------------------
# _deliver — HMAC header present and correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_sends_correct_hmac_header(db_path):
    from detection.webhook_queue import enqueue, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    secret = "whsec_test_secret"
    sub_id = register_subscriber("https://example.com/webhook", secret, db_path=db_path)
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    captured = {}

    async def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _deliver(client, deliveries[0], sub, db_path=db_path)

    raw_headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-ledgerlens-signature" in raw_headers
    sig = raw_headers["x-ledgerlens-signature"]
    body = captured["body"]

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == expected


# ---------------------------------------------------------------------------
# _deliver — receiver response body discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_discards_response_body(db_path):
    from detection.webhook_queue import enqueue, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    sub_id = register_subscriber("https://example.com/webhook", "whsec_secret", db_path=db_path)
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(200, content=b"<script>alert(1)</script>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is True


# ---------------------------------------------------------------------------
# Concurrency: run_delivery_worker processes at most 10 concurrent deliveries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_concurrency_limit(db_path):
    from detection.webhook_queue import enqueue, get_due_deliveries
    from detection.webhook_registry import get_subscriber, register_subscriber
    from detection.webhook_worker import _deliver

    sub_id = register_subscriber("https://example.com/webhook", "whsec_secret", db_path=db_path)
    for i in range(15):
        enqueue(sub_id, {"wallet": f"G{i}", "score": 85}, db_path)

    deliveries = get_due_deliveries(limit=15, db_path=db_path)
    assert len(deliveries) == 15

    inflight = 0
    max_inflight = 0
    lock = asyncio.Lock()

    async def handler(request):
        nonlocal inflight, max_inflight
        async with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        async with lock:
            inflight -= 1
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        semaphore = asyncio.Semaphore(10)

        async def _deliver_one(d):
            async with semaphore:
                sub = get_subscriber(d.subscriber_id, db_path=db_path)
                if sub and sub.active:
                    await _deliver(client, d, sub, db_path=db_path)

        await asyncio.gather(*[_deliver_one(d) for d in deliveries])

    assert max_inflight <= 10, f"Expected max 10 concurrent, got {max_inflight}"
