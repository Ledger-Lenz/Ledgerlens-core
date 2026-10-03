"""End-to-end tests for webhook signature verification, retry backoff and dead-lettering."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os

import httpx
import pytest

import api.webhook_sender as sender
import detection.webhook_queue as queue
from detection.webhook_verify import main as verify_cli
from detection.webhook_verify import verify_signature, verify_webhook
from detection.webhook_worker import _deliver

SECRET = "e2e_secret_do_not_use"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(
        "detection.webhook_registry._resolve_hostname", lambda hostname: "93.184.216.34"
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "webhook_e2e.db")
    from detection.webhook_registry import init_db

    init_db(path)
    queue.init_db(path)
    return path


def _subscriber(db_path):
    from detection.webhook_registry import get_subscriber, register_subscriber

    sub_id = register_subscriber(
        url="https://example.com/hook", secret=SECRET, min_score=0, db_path=db_path
    )
    return get_subscriber(sub_id, db_path=db_path)


def _run_delivery(db_path, handler):
    sub = _subscriber(db_path)
    queue.enqueue(sub.subscriber_id, {"wallet": "GABC", "score": 91}, db_path)
    delivery = queue.get_due_deliveries(db_path=db_path)[0]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _deliver(client, delivery, sub, db_path=db_path)

    return asyncio.run(run()), delivery


# --- signature verification against real generated signatures ------------------


def test_reference_verifier_accepts_real_delivery(db_path):
    captured = {}

    def handler(request):
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    ok, _ = _run_delivery(db_path, handler)
    assert ok
    assert verify_webhook(captured["body"], captured["headers"], SECRET)
    assert not verify_webhook(captured["body"] + b" ", captured["headers"], SECRET)
    assert not verify_webhook(captured["body"], captured["headers"], "wrong-secret")
    stale = int(captured["headers"]["x-ledgerlens-timestamp"]) + 3600
    assert not verify_webhook(captured["body"], captured["headers"], SECRET, now=stale)


def test_reference_cli(db_path, monkeypatch, capsys):
    captured = {}

    def handler(request):
        captured["body"] = request.content
        captured["sig"] = request.headers["x-ledgerlens-signature"]
        return httpx.Response(204)

    _run_delivery(db_path, handler)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(captured["body"])))
    assert verify_cli(["--secret", SECRET, "--signature", captured["sig"]]) == 0
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert verify_cli(["--secret", SECRET, "--signature", captured["sig"]]) == 1
    assert capsys.readouterr().out.split() == ["valid", "invalid"]


def test_verify_signature_rejects_malformed_header():
    assert not verify_signature(b"{}", "", SECRET)
    assert not verify_signature(b"{}", "md5=abc", SECRET)


# --- retry backoff timing ------------------------------------------------------


@pytest.mark.parametrize(
    "attempt,jitter,expected",
    [(1, 0.0, 10), (1, 1.0, 8), (3, 0.5, 36), (12, 0.0, 3600), (12, 1.0, 2880)],
)
def test_queue_backoff_with_jitter(monkeypatch, attempt, jitter, expected):
    monkeypatch.setattr(queue, "_jitter", lambda: jitter)
    assert queue.compute_backoff(attempt) == pytest.approx(expected)


def test_failed_delivery_schedules_jittered_retry(db_path, monkeypatch):
    monkeypatch.setattr(queue, "_jitter", lambda: 1.0)
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    ok, delivery = _run_delivery(db_path, lambda r: httpx.Response(503))
    assert not ok
    with queue._connect(db_path) as conn:
        next_at, status = conn.execute(
            "SELECT next_attempt_at, status FROM webhook_delivery_queue WHERE id = ?",
            (delivery.id,),
        ).fetchone()
    delay = (datetime.fromisoformat(next_at) - before).total_seconds()
    assert status == "pending"
    assert 8 <= delay < 9  # 2^1 * 5s minus the full 20% jitter


def test_sender_retry_schedule_and_dlq(tmp_path, monkeypatch, caplog):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(sender.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sender.random, "random", lambda: 0.5)
    monkeypatch.setattr(
        sender.httpx,
        "AsyncClient",
        lambda: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    db = str(tmp_path / "dlq.db")
    rq = sender.WebhookRetryQueue(db_path=db)
    with caplog.at_level(logging.ERROR, logger="ledgerlens.webhook.sender"):
        asyncio.run(rq._run_with_retries("sub-1", "https://example.com/hook", SECRET, {"a": 1}))

    assert sleeps == pytest.approx([27, 270, 1620])  # 10% jitter at r=0.5
    [entry] = sender.list_dlq(db_path=db)
    assert entry.attempt_count == 3 and entry.last_error == "HTTP 500"
    assert "webhook.dead_lettered" in caplog.text


# --- dead-letter routing -------------------------------------------------------


def test_persistently_failing_endpoint_is_dead_lettered_with_alert(db_path, caplog):
    sub = _subscriber(db_path)
    queue.enqueue(sub.subscriber_id, {"wallet": "GABC", "score": 91}, db_path)
    delivery = queue.get_due_deliveries(db_path=db_path)[0]
    with queue._connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET attempt_count = ? WHERE id = ?",
            (queue.MAX_ATTEMPTS - 1, delivery.id),
        )
        conn.commit()
    delivery.attempt_count = queue.MAX_ATTEMPTS - 1

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            return await _deliver(client, delivery, sub, db_path=db_path)

    with caplog.at_level(logging.ERROR, logger="ledgerlens.webhook.queue"):
        assert asyncio.run(run()) is False

    [dead] = queue.get_dead_letters(db_path=db_path)
    assert dead.id == delivery.id and dead.attempt_count == queue.MAX_ATTEMPTS
    assert queue.get_due_deliveries(db_path=db_path) == []
    assert "webhook.dead_lettered" in caplog.text
    assert json.loads(dead.payload_json)["score"] == 91
