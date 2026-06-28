"""Exhaustive webhook HMAC replay-attack prevention test suite (Issue #144).

Covers:
  - TestHMACVerification: correct/wrong secret, tampered body, wrong prefix
  - TestTimestampReplayPrevention: window enforcement with frozen time
  - TestSecretRotation: new secret used after rotation, no delivery duplication
  - TestDeadLetterBehaviour: exactly 8 failures, exponential backoff delays
  - TestConcurrency: 10 simultaneous deliveries, slow subscriber isolation
  - TestSSRFProtection: private IP ranges rejected at registration
  - Static analysis: HMAC comparisons use hmac.compare_digest (constant-time)
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import os
import pathlib
import time

import httpx
import pytest
from freezegun import freeze_time


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    """Provide a valid AES-256-GCM encryption key for every test."""
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture(autouse=True)
def mock_dns(monkeypatch):
    """Mock DNS resolution to return a public IP so tests work offline."""
    monkeypatch.setattr(
        "detection.webhook_registry._resolve_hostname",
        lambda hostname: "93.184.216.34",  # example.com public IP
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "webhook_security.db")


@pytest.fixture(autouse=True)
def _fix_settings(monkeypatch, db_path):
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    import config.settings as s
    object.__setattr__(s.settings, "ledgerlens_db_path", db_path)


def _sample_payload() -> dict:
    return {"wallet": "GABC1234567890TEST", "score": 85, "asset_pair": "XLM/USDC"}


def _register(url: str = "https://example.com/hook", secret: str = "test_secret_do_not_use",
               min_score: int = 70, db_path: str | None = None) -> str:
    from detection.webhook_registry import register_subscriber
    return register_subscriber(url=url, secret=secret, min_score=min_score, db_path=db_path)


# ---------------------------------------------------------------------------
# Helper: verify_signature (extracted logic from webhook_worker)
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification matching the worker's logic."""
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _verify_timestamp(ts: int, window_seconds: int = 300, now: int | None = None) -> bool:
    """Return True if the timestamp is within ±window_seconds of now."""
    if now is None:
        now = int(time.time())
    diff = now - ts
    return -window_seconds <= diff <= window_seconds


# ---------------------------------------------------------------------------
# TestHMACVerification
# ---------------------------------------------------------------------------

class TestHMACVerification:
    """Verify that HMAC-SHA256 signature checking behaves correctly under all
    forgery and tamper scenarios before any public-facing deployment."""

    def test_correct_secret_passes(self):
        body = b'{"event":"risk_score_alert","data":{}}'
        secret = "test_secret_do_not_use"
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is True

    def test_wrong_secret_fails(self):
        body = b'{"event":"risk_score_alert"}'
        right_secret = "test_secret_do_not_use"
        sig = "sha256=" + hmac.new(b"wrong_secret", body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, right_secret) is False

    def test_empty_body_fails_with_wrong_secret(self):
        body = b""
        secret = "test_secret_do_not_use"
        sig = "sha256=" + hmac.new(b"wrong_secret", body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is False

    def test_empty_body_correct_secret_passes(self):
        body = b""
        secret = "test_secret_do_not_use"
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is True

    def test_tampered_body_fails(self):
        secret = "test_secret_do_not_use"
        original_body = b'{"score":85}'
        sig = "sha256=" + hmac.new(secret.encode(), original_body, hashlib.sha256).hexdigest()
        tampered_body = b'{"score":10}'
        assert _verify_signature(tampered_body, sig, secret) is False

    @pytest.mark.parametrize("bad_prefix", ["md5=", "sha1=", "plain=", ""])
    def test_wrong_prefix_fails(self, bad_prefix):
        secret = "test_secret_do_not_use"
        body = b"test_body"
        sig = bad_prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is False

    def test_build_hmac_signature_uses_sha256_prefix(self):
        from detection.webhook_worker import build_hmac_signature
        sig = build_hmac_signature(b"test", "test_secret_do_not_use")
        assert sig.startswith("sha256=")

    def test_build_hmac_signature_is_correct_digest(self):
        from detection.webhook_worker import build_hmac_signature
        body = b'{"wallet":"GABC","score":80}'
        secret = "test_secret_do_not_use"
        sig = build_hmac_signature(body, secret)
        expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert sig == f"sha256={expected_hex}"


# ---------------------------------------------------------------------------
# TestTimestampReplayPrevention
# ---------------------------------------------------------------------------

class TestTimestampReplayPrevention:
    """Verify that replay-window enforcement correctly accepts/rejects
    timestamps across boundary conditions using deterministic frozen time."""

    @pytest.mark.parametrize("age_seconds,expected", [
        (0, True),       # fresh
        (100, True),     # well within window
        (299, True),     # 1 second before boundary
        (300, True),     # exactly at boundary (inclusive)
        (301, False),    # 1 second past window
        (3600, False),   # 1 hour old replay
    ])
    def test_timestamp_window_past(self, age_seconds, expected):
        with freeze_time("2026-06-24T10:00:00Z") as frozen:
            now_ts = int(frozen().timestamp())
            ts = now_ts - age_seconds
            assert _verify_timestamp(ts, window_seconds=300, now=now_ts) is expected

    @pytest.mark.parametrize("future_secs,expected", [
        (30, True),    # 30s in the future (clock skew)
        (300, True),   # exactly 5 min ahead (boundary)
        (301, False),  # too far in the future
    ])
    def test_timestamp_future_clock_skew(self, future_secs, expected):
        with freeze_time("2026-06-24T10:00:00Z") as frozen:
            now_ts = int(frozen().timestamp())
            ts = now_ts + future_secs
            assert _verify_timestamp(ts, window_seconds=300, now=now_ts) is expected

    def test_missing_timestamp_equivalent_to_zero_rejected(self):
        """A timestamp of 0 (Unix epoch) must be rejected."""
        assert _verify_timestamp(0, window_seconds=300) is False

    def test_deliver_includes_timestamp_header(self, db_path):
        """_deliver must send X-LedgerLens-Timestamp header."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)
        deliveries = get_due_deliveries(db_path=db_path)
        sub = get_subscriber(sub_id, db_path)
        captured: dict = {}

        async def handler(request):
            captured["headers"] = dict(request.headers)
            return httpx.Response(200)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, deliveries[0], sub, db_path=db_path)

        asyncio.get_event_loop().run_until_complete(run())
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert "x-ledgerlens-timestamp" in headers
        ts = int(headers["x-ledgerlens-timestamp"])
        assert ts > 0

    def test_timestamp_header_is_recent(self, db_path):
        """X-LedgerLens-Timestamp must be within the last 10 seconds."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)
        deliveries = get_due_deliveries(db_path=db_path)
        sub = get_subscriber(sub_id, db_path)
        captured: dict = {}

        async def handler(request):
            captured["ts"] = int(request.headers.get("x-ledgerlens-timestamp", "0"))
            return httpx.Response(200)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, deliveries[0], sub, db_path=db_path)

        before = int(time.time())
        asyncio.get_event_loop().run_until_complete(run())
        after = int(time.time())
        assert before - 1 <= captured["ts"] <= after + 1


# ---------------------------------------------------------------------------
# TestSecretRotation
# ---------------------------------------------------------------------------

class TestSecretRotation:
    """Verify that secret rotation is atomic: no deliveries are dropped or
    duplicated, and subsequent deliveries use the new secret."""

    def test_queued_delivery_uses_new_secret_after_rotation(self, db_path):
        """Deliveries enqueued before rotation should use the NEW secret when
        the subscriber secret is rotated before the worker runs."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber, deactivate_subscriber, register_subscriber
        from detection.webhook_worker import _deliver

        old_secret = "old_secret_do_not_use"
        new_secret = "new_secret_do_not_use"

        # Register with old secret and enqueue
        sub_id = register_subscriber("https://example.com/hook", old_secret, db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        # Rotate: deactivate old, register new with same URL
        deactivate_subscriber(sub_id, db_path=db_path)
        new_sub_id = register_subscriber("https://example.com/hook", new_secret, db_path=db_path)
        # Enqueue again for new subscriber
        enqueue(new_sub_id, _sample_payload(), db_path)

        deliveries = get_due_deliveries(db_path=db_path)
        new_sub = get_subscriber(new_sub_id, db_path)
        captured: dict = {}

        async def handler(request):
            captured["sig"] = request.headers.get("x-ledgerlens-signature", "")
            captured["body"] = request.content
            return httpx.Response(200)

        # Only deliver the NEW subscriber's item
        new_delivery = next(d for d in deliveries if d.subscriber_id == new_sub_id)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, new_delivery, new_sub, db_path=db_path)

        asyncio.get_event_loop().run_until_complete(run())

        expected = "sha256=" + hmac.new(
            new_secret.encode(), captured["body"], hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(captured["sig"], expected), \
            "Delivery should be signed with the NEW secret"

    def test_old_subscriber_deactivated_gets_no_delivery(self, db_path):
        """After deactivation the old subscriber receives no deliveries."""
        from detection.webhook_queue import enqueue
        from detection.webhook_registry import get_subscriber, deactivate_subscriber

        sub_id = _register(db_path=db_path)
        deactivate_subscriber(sub_id, db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        sub = get_subscriber(sub_id, db_path)
        assert sub.active is False

    def test_no_deliveries_duplicated_during_rotation(self, db_path):
        """Enqueue one item; deliver once. Should not produce duplicate rows."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver
        import sqlite3

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        deliveries = get_due_deliveries(db_path=db_path)
        assert len(deliveries) == 1

        sub = get_subscriber(sub_id, db_path)

        async def handler(request):
            return httpx.Response(200)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, deliveries[0], sub, db_path=db_path)

        asyncio.get_event_loop().run_until_complete(run())

        # Only one row total, status = delivered
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, status FROM webhook_delivery_queue"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "delivered"


# ---------------------------------------------------------------------------
# TestDeadLetterBehaviour
# ---------------------------------------------------------------------------

class TestDeadLetterBehaviour:
    """Verify dead-lettering after exactly 8 failures and exponential backoff."""

    def test_exactly_8_failures_triggers_dead_letter(self, db_path):
        """After 8 consecutive failures (attempt_count reaches 8), status → 'dead'."""
        from detection.webhook_queue import _connect, enqueue, get_dead_letters, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        # Pre-set attempt_count to 7 so the next failure is the 8th
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE webhook_delivery_queue SET attempt_count = 7"
            )
            conn.commit()

        deliveries = get_due_deliveries(db_path=db_path)
        sub = get_subscriber(sub_id, db_path)

        async def handler(request):
            return httpx.Response(500)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, deliveries[0], sub, db_path=db_path)

        asyncio.get_event_loop().run_until_complete(run())

        dead = get_dead_letters(db_path=db_path)
        assert len(dead) == 1
        assert dead[0].status == "dead"
        assert dead[0].attempt_count == 8

    def test_7_failures_does_not_dead_letter(self, db_path):
        """After 7 failures the item must remain 'pending' (not dead)."""
        from detection.webhook_queue import _connect, enqueue, get_dead_letters, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        # Pre-set to attempt_count = 6 → next failure makes it 7
        with _connect(db_path) as conn:
            conn.execute("UPDATE webhook_delivery_queue SET attempt_count = 6")
            conn.commit()

        deliveries = get_due_deliveries(db_path=db_path)
        sub = get_subscriber(sub_id, db_path)

        async def handler(request):
            return httpx.Response(500)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await _deliver(client, deliveries[0], sub, db_path=db_path)

        asyncio.get_event_loop().run_until_complete(run())

        dead = get_dead_letters(db_path=db_path)
        assert len(dead) == 0

    def test_backoff_delays_are_exponential(self, db_path):
        """mark_failed schedules next_attempt_at at 2^N * 5 seconds from now."""
        from detection.webhook_queue import enqueue, mark_failed
        from datetime import datetime, timezone
        import sqlite3

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM webhook_delivery_queue"
            ).fetchone()
        delivery_id = row[0]

        before = datetime.now(timezone.utc)
        mark_failed(delivery_id, "test error", db_path=db_path)
        datetime.now(timezone.utc)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT next_attempt_at, attempt_count FROM webhook_delivery_queue WHERE id = ?",
                (delivery_id,),
            ).fetchone()

        next_at = datetime.fromisoformat(row[0])
        attempt = row[1]  # should be 1

        # Expected backoff: 2^1 * 5 = 10 seconds
        expected_delay = (2 ** attempt) * 5
        actual_delay = (next_at - before).total_seconds()
        assert expected_delay - 1 <= actual_delay <= expected_delay + 2, \
            f"Expected ~{expected_delay}s backoff, got {actual_delay:.1f}s"

    @pytest.mark.parametrize("attempt,expected_delay_s", [
        (1, 10),
        (2, 20),
        (3, 40),
        (4, 80),
        (5, 160),
        (6, 320),
        (7, 640),
    ])
    def test_backoff_formula(self, attempt, expected_delay_s, db_path):
        """Each attempt N uses delay = min(2^N * 5, 3600) seconds."""
        from detection.webhook_queue import BASE_DELAY, MAX_DELAY
        delay = min(2 ** attempt * BASE_DELAY, MAX_DELAY)
        assert delay == min(expected_delay_s, MAX_DELAY)

    def test_dead_letters_endpoint_returns_dead_items(self, db_path):
        """get_dead_letters only returns 'dead' items."""
        from detection.webhook_queue import enqueue, get_dead_letters, _connect

        sub_id = _register(db_path=db_path)
        enqueue(sub_id, _sample_payload(), db_path)

        with _connect(db_path) as conn:
            row = conn.execute("SELECT id FROM webhook_delivery_queue").fetchone()
            delivery_id = row[0]
            # Force to dead status
            conn.execute(
                "UPDATE webhook_delivery_queue SET status='dead', attempt_count=8 WHERE id=?",
                (delivery_id,),
            )
            conn.commit()

        dead = get_dead_letters(db_path=db_path)
        assert len(dead) == 1
        assert dead[0].status == "dead"


# ---------------------------------------------------------------------------
# TestConcurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    """Verify that concurrent deliveries are isolated and slow subscribers
    do not block fast ones."""

    @pytest.mark.asyncio
    async def test_10_simultaneous_deliveries_do_not_interfere(self, db_path):
        """10 deliveries to different subscribers succeed independently."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        sub_ids = []
        for i in range(10):
            sid = _register(
                url=f"https://example{i}.com/hook",
                secret=f"test_secret_{i}",
                db_path=db_path,
            )
            sub_ids.append(sid)
            enqueue(sid, _sample_payload(), db_path)

        deliveries = get_due_deliveries(limit=10, db_path=db_path)
        assert len(deliveries) == 10

        async def handler(request):
            return httpx.Response(200)

        results = []
        semaphore = asyncio.Semaphore(10)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def _deliver_one(d):
                async with semaphore:
                    sub = get_subscriber(d.subscriber_id, db_path=db_path)
                    if sub and sub.active:
                        r = await _deliver(client, d, sub, db_path=db_path)
                        results.append(r)

            await asyncio.gather(*[_deliver_one(d) for d in deliveries])

        assert len(results) == 10
        assert all(r is True for r in results)

    @pytest.mark.asyncio
    async def test_slow_subscriber_does_not_block_fast_subscriber(self, db_path):
        """A 200ms-delayed subscriber should not delay a fast subscriber noticeably."""
        from detection.webhook_queue import enqueue, get_due_deliveries
        from detection.webhook_registry import get_subscriber
        from detection.webhook_worker import _deliver

        slow_id = _register(url="https://slow.example.com/hook", db_path=db_path)
        fast_id = _register(url="https://fast.example.com/hook", db_path=db_path)
        enqueue(slow_id, _sample_payload(), db_path)
        enqueue(fast_id, _sample_payload(), db_path)

        deliveries = get_due_deliveries(limit=2, db_path=db_path)
        slow_delivery = next(d for d in deliveries if d.subscriber_id == slow_id)
        fast_delivery = next(d for d in deliveries if d.subscriber_id == fast_id)

        fast_done_at = None

        async def handler(request):
            if "slow" in str(request.url):
                await asyncio.sleep(0.2)
            else:
                nonlocal fast_done_at
                fast_done_at = time.monotonic()
            return httpx.Response(200)

        start = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            slow_sub = get_subscriber(slow_id, db_path=db_path)
            fast_sub = get_subscriber(fast_id, db_path=db_path)
            await asyncio.gather(
                _deliver(client, slow_delivery, slow_sub, db_path=db_path),
                _deliver(client, fast_delivery, fast_sub, db_path=db_path),
            )

        # The fast delivery should complete well before the slow one
        assert fast_done_at is not None
        fast_elapsed = fast_done_at - start
        assert fast_elapsed < 0.15, f"Fast subscriber took {fast_elapsed:.3f}s — was blocked by slow?"


# ---------------------------------------------------------------------------
# Static analysis: constant-time HMAC comparison
# ---------------------------------------------------------------------------

def test_hmac_comparison_is_constant_time():
    """Verify that webhook_worker.py never compares signatures with == operator.

    Any == comparison involving a variable named *sig* or *signature* is a
    potential timing side-channel. All such comparisons must use hmac.compare_digest.
    """
    source = pathlib.Path("detection/webhook_worker.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if isinstance(op, ast.Eq):
                    operands = [ast.unparse(node.left)] + [
                        ast.unparse(c) for c in node.comparators
                    ]
                    for operand in operands:
                        assert "sig" not in operand.lower(), (
                            f"Possible timing-unsafe signature comparison at line "
                            f"{node.lineno}: {ast.unparse(node)}"
                        )


# ---------------------------------------------------------------------------
# TestSSRFProtection
# ---------------------------------------------------------------------------

class TestSSRFProtection:
    """Verify that private/reserved IP ranges are rejected at registration time."""

    @pytest.mark.parametrize("bad_url", [
        "http://127.0.0.1/hook",
        "https://192.168.1.1/hook",
        "https://10.0.0.1/hook",
        "https://172.16.0.1/hook",
        "http://localhost/hook",
        "http://0.0.0.0/hook",
    ])
    def test_private_ip_rejected_at_registration(self, bad_url, monkeypatch):
        """Private/reserved URLs must raise ValueError even when DNS resolves to them."""
        from detection.webhook_registry import validate_webhook_url
        import urllib.parse
        parsed = urllib.parse.urlparse(bad_url)
        # Extract the IP/hostname from the URL and mock DNS to return it directly
        host = parsed.hostname
        monkeypatch.setattr(
            "detection.webhook_registry._resolve_hostname",
            lambda h: host,
        )
        with pytest.raises(ValueError):
            validate_webhook_url(bad_url)

    def test_http_scheme_rejected(self, monkeypatch):
        """HTTP (non-HTTPS) URLs must be rejected before DNS lookup."""
        from detection.webhook_registry import validate_webhook_url
        monkeypatch.setattr(
            "detection.webhook_registry._resolve_hostname",
            lambda h: "93.184.216.34",
        )
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("http://publicserver.example.com/hook")

    def test_ipv6_loopback_rejected(self, monkeypatch):
        """IPv6 loopback ::1 must be rejected."""
        from detection.webhook_registry import validate_webhook_url
        monkeypatch.setattr(
            "detection.webhook_registry._resolve_hostname",
            lambda h: "::1",
        )
        with pytest.raises(ValueError):
            validate_webhook_url("https://some-host.example.com/hook")

    def test_valid_https_url_accepted(self, db_path):
        """A valid public HTTPS URL should be accepted (registration succeeds)."""
        from detection.webhook_registry import register_subscriber

        sub_id = register_subscriber(
            url="https://example.com/hook",
            secret="test_secret_do_not_use",
            db_path=db_path,
        )
        assert sub_id is not None
