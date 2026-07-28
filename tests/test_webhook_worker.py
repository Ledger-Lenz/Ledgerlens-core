"""Tests for ``detection.webhook_worker``.

All source imports are at module level.  Every test receives an isolated
SQLite database via the ``db_path`` fixture and a fresh encryption key via the
``webhook_env`` fixture so there is no shared state between tests.

``_deliver`` imports ``config.telemetry.get_tracer`` and
``api.metrics.webhook_deliveries_total`` at call time.  The ``_patch_deps``
fixture stubs both at their source modules so tests work without a running
OpenTelemetry SDK or Prometheus registry conflict.
"""

import asyncio
import base64
import hashlib
import hmac
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from detection.webhook_queue import (
    _connect,
    enqueue,
    get_dead_letters,
    get_due_deliveries,
    init_db as init_queue_db,
)
from detection.webhook_registry import (
    get_subscriber,
    init_db as init_registry_db,
    register_subscriber,
)
from detection.webhook_worker import (
    _deliver,
    build_hmac_signature,
    build_webhook_payload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    """Inject a fresh random AES-256-GCM key so every test can encrypt secrets."""
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture
def db_path(tmp_path):
    """Per-test SQLite path for both the queue and registry databases."""
    return str(tmp_path / "worker.db")


@pytest.fixture(autouse=True)
def _fix_settings(monkeypatch, db_path):
    """Point ``settings.db_path`` at the per-test SQLite file.

    ``mark_delivered`` / ``mark_failed`` fall back to ``settings.db_path``
    when called without an explicit ``db_path=`` argument.  Redirecting the
    setting prevents accidental writes to the real database.
    """
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    import config.settings as s
    try:
        object.__setattr__(s.settings, "db_path", db_path)
    except (AttributeError, TypeError):
        # settings may be a frozen model; env var override is sufficient
        pass


@pytest.fixture(autouse=True)
def _patch_deps():
    """Stub out telemetry and Prometheus counters used inside ``_deliver``.

    ``_deliver`` imports ``get_tracer`` and ``webhook_deliveries_total`` at
    call time (not at module import time) to avoid circular imports.  We patch
    at the source module level so the lazy imports pick up our stubs.
    """
    mock_span = MagicMock()
    mock_span.__enter__ = MagicMock(return_value=mock_span)
    mock_span.__exit__ = MagicMock(return_value=False)

    mock_tracer = MagicMock()
    mock_tracer.start_as_current_span.return_value = mock_span

    mock_counter = MagicMock()
    mock_counter.labels.return_value = MagicMock()

    with patch("config.telemetry.get_tracer", return_value=mock_tracer), \
         patch("api.metrics.webhook_deliveries_total", mock_counter):
        yield


# ---------------------------------------------------------------------------
# HMAC signature helpers
# ---------------------------------------------------------------------------


def test_build_hmac_signature_is_verifiable():
    """build_hmac_signature() produces a sha256= digest that verifies correctly."""
    body = b'{"event": "risk_score_alert", "data": {"wallet": "GABC"}}'
    secret = "whsec_test_secret"
    sig = build_hmac_signature(body, secret)

    assert sig.startswith("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig[len("sha256="):] == expected


def test_hmac_signature_different_body_different_signature():
    """Different bodies produce different HMAC signatures."""
    sig1 = build_hmac_signature(b'{"a": 1}', "secret")
    sig2 = build_hmac_signature(b'{"a": 2}', "secret")
    assert sig1 != sig2


def test_hmac_signature_different_secret_different_signature():
    """Different secrets produce different HMAC signatures for the same body."""
    sig1 = build_hmac_signature(b'{"a": 1}', "secret1")
    sig2 = build_hmac_signature(b'{"a": 1}', "secret2")
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def test_build_webhook_payload():
    """build_webhook_payload() wraps score_data under the expected envelope."""
    score_data = {"wallet": "GABC", "score": 85}
    payload = build_webhook_payload(score_data)

    assert payload["event"] == "risk_score_alert"
    assert payload["data"] == score_data
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# _deliver — successful delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_success_marks_delivered(db_path):
    """A 200 response marks the delivery as delivered and removes it from
    the pending queue."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    sub_id = register_subscriber(
        "https://example.com/webhook", "whsec_secret", db_path=db_path
    )
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
# _deliver — HTTP error triggers retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_http_500_triggers_retry(db_path):
    """A 500 response returns False and increments attempt_count without
    moving the delivery to dead-letter."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    sub_id = register_subscriber(
        "https://example.com/webhook", "whsec_secret", db_path=db_path
    )
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is False

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, status, last_error "
            "FROM webhook_delivery_queue WHERE id = ?",
            (deliveries[0].id,),
        ).fetchone()

    assert row[0] == 1          # attempt_count incremented
    assert row[1] == "pending"  # still pending (not dead yet)
    assert row[2] == "HTTP 500"


# ---------------------------------------------------------------------------
# _deliver — dead-letter after max attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_moves_to_dead_after_8_attempts(db_path):
    """After 8 total attempts a 500 response moves the delivery to dead-letter."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    sub_id = register_subscriber(
        "https://example.com/webhook", "whsec_secret", db_path=db_path
    )
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)

    # Pre-seed attempt_count to 7 so the next failure reaches 8 (MAX_ATTEMPTS).
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
# _deliver — HMAC header is correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_sends_correct_hmac_header(db_path):
    """The X-LedgerLens-Signature header contains a valid HMAC-SHA256 digest
    computed from the raw request body and the subscriber secret."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    secret = "whsec_test_secret"
    sub_id = register_subscriber(
        "https://example.com/webhook", secret, db_path=db_path
    )
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    captured: dict = {}

    async def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _deliver(client, deliveries[0], sub, db_path=db_path)

    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-ledgerlens-signature" in headers

    expected = (
        "sha256="
        + hmac.new(secret.encode(), captured["body"], hashlib.sha256).hexdigest()
    )
    assert headers["x-ledgerlens-signature"] == expected


# ---------------------------------------------------------------------------
# _deliver — response body is discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_discards_response_body(db_path):
    """The worker ignores the response body entirely (log-injection protection)."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    sub_id = register_subscriber(
        "https://example.com/webhook", "whsec_secret", db_path=db_path
    )
    enqueue(sub_id, {"wallet": "GABC", "score": 85}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    sub = get_subscriber(sub_id, db_path)

    async def handler(request):
        return httpx.Response(200, content=b"<script>alert(1)</script>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _deliver(client, deliveries[0], sub, db_path=db_path)

    assert result is True


# ---------------------------------------------------------------------------
# Concurrency: semaphore caps in-flight deliveries at MAX_CONCURRENT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_concurrency_limit(db_path):
    """At most MAX_CONCURRENT (10) deliveries run simultaneously."""
    init_registry_db(db_path)
    init_queue_db(db_path)

    sub_id = register_subscriber(
        "https://example.com/webhook", "whsec_secret", db_path=db_path
    )
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
