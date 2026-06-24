---
title: "Implement Exhaustive Webhook HMAC Replay-Attack Prevention Test Suite"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary

Extend the test suite for `detection/webhook_registry.py` and `detection/webhook_worker.py` with a comprehensive, exhaustive test suite covering: HMAC-SHA256 verification with correct and incorrect secrets, timestamp replay-window enforcement (reject payloads older than 5 minutes), secret rotation without delivery interruption, and dead-letter behaviour after 8 consecutive failures. This suite hardens the webhook subsystem against the most critical security and reliability failure modes before any public-facing deployment.

## Background & Context

LedgerLens's webhook system allows protocol teams and asset issuers to receive real-time risk-score alerts via signed HTTP POST requests. Each delivery is signed with an HMAC-SHA256 digest over the raw request body, keyed with the subscriber's secret. The `X-LedgerLens-Timestamp` header enables replay-attack prevention: receivers should reject payloads with timestamps older than 5 minutes.

The existing test coverage for `detection/webhook_registry.py` and `detection/webhook_worker.py` is minimal — primarily happy-path delivery. Missing coverage includes:

- **Signature forgery**: what happens when a delivery is intercepted and re-sent with a modified body but the original signature?
- **Replay attack**: what happens when a valid signed payload is re-sent after 6 minutes?
- **Secret rotation**: can a subscriber rotate their HMAC secret without dropping in-flight or queued deliveries?
- **Dead-letter accumulation**: does the system correctly move a subscriber to dead-letter status after exactly 8 failures, no more, no fewer?
- **Timing side-channels**: is HMAC comparison done with `hmac.compare_digest()` (constant-time) rather than `==` (variable-time)?

This issue produces a comprehensive test file `tests/test_webhook_security.py` plus extensions to `tests/test_webhook_worker.py`. All tests must be deterministic (no real HTTP calls, no real clock; mock both), runnable with `pytest`, and must not depend on external services.

## Objectives

- [ ] Implement `TestHMACVerification` class with tests for: correct secret → verification passes; wrong secret → verification fails; empty body → verification fails; body tampered post-signing → verification fails; signature with wrong prefix (`md5=` instead of `sha256=`) → fails.
- [ ] Implement `TestTimestampReplayPrevention` with tests for: timestamp within 5-min window → accepted; timestamp exactly 5 min ago → accepted (boundary); timestamp 5 min + 1 sec ago → rejected; future timestamp (clock skew) → accepted if within 5 min ahead; missing `X-LedgerLens-Timestamp` header → rejected.
- [ ] Implement `TestSecretRotation` with tests for: rotate secret while delivery is queued → new secret used for subsequent deliveries; in-flight delivery with old secret completes successfully; no deliveries are dropped or duplicated during rotation.
- [ ] Implement `TestDeadLetterBehaviour` with tests for: exactly 8 failures → subscriber moved to `dead` status; 7 failures → subscriber remains `active`; verify exponential backoff delays between retries (2^n × 5s); verify `GET /webhooks/dead-letters` lists the subscriber.
- [ ] Implement `TestConcurrency` with tests for: 10 simultaneous deliveries to different subscribers do not interfere; slow subscriber (mock 10s response) does not block fast subscriber.
- [ ] Verify that all HMAC comparisons in `webhook_worker.py` use `hmac.compare_digest()`, not `==`; add a static analysis test using AST inspection.
- [ ] Add `TestSSRFProtection` verifying that subscriber URLs pointing to private IP ranges (`127.0.0.1`, `10.x`, `192.168.x`, `::1`) are rejected at registration with HTTP 422.
- [ ] All tests are parameterised where appropriate using `pytest.mark.parametrize`.
- [ ] Zero external HTTP calls; all HTTP mocked via `respx` or `httpx` mock transport.

## Technical Requirements

### Test file structure

```
tests/
├── test_webhook_security.py       # HMAC, replay, secret rotation, SSRF
├── test_webhook_worker.py         # Dead-letter, backoff, concurrency (extend existing)
└── conftest.py                    # Shared fixtures: mock DB, mock clock, mock HTTP
```

### HMAC verification tests

```python
import hmac, hashlib, pytest

class TestHMACVerification:
    def test_correct_secret_passes(self, worker, subscriber):
        body = b'{"event":"risk_score_alert","data":{}}'
        sig = "sha256=" + hmac.new(subscriber.secret.encode(), body, hashlib.sha256).hexdigest()
        assert worker.verify_signature(body, sig, subscriber.secret) is True

    def test_wrong_secret_fails(self, worker, subscriber):
        body = b'{"event":"risk_score_alert"}'
        sig = "sha256=" + hmac.new(b"wrong_secret", body, hashlib.sha256).hexdigest()
        assert worker.verify_signature(body, sig, subscriber.secret) is False

    def test_tampered_body_fails(self, worker, subscriber):
        original_body = b'{"score":85}'
        sig = "sha256=" + hmac.new(subscriber.secret.encode(), original_body, hashlib.sha256).hexdigest()
        tampered_body = b'{"score":10}'
        assert worker.verify_signature(tampered_body, sig, subscriber.secret) is False

    @pytest.mark.parametrize("bad_prefix", ["md5=", "sha1=", "plain=", ""])
    def test_wrong_prefix_fails(self, worker, subscriber, bad_prefix):
        body = b'test'
        sig = bad_prefix + hmac.new(subscriber.secret.encode(), body, hashlib.sha256).hexdigest()
        assert worker.verify_signature(body, sig, subscriber.secret) is False
```

### Replay prevention tests

```python
from freezegun import freeze_time

class TestTimestampReplayPrevention:
    @pytest.mark.parametrize("age_seconds,expected", [
        (0, True),
        (299, True),
        (300, True),        # boundary: exactly 5 min
        (301, False),       # 1 second past window
        (3600, False),      # 1 hour old
        (-30, True),        # 30s in the future (clock skew tolerance)
        (-301, False),      # too far in the future
    ])
    def test_timestamp_window(self, worker, age_seconds, expected):
        with freeze_time("2026-06-24T10:00:00Z") as frozen_time:
            ts = int((frozen_time().timestamp()) - age_seconds)
            assert worker.verify_timestamp(ts, window_seconds=300) is expected
```

### Secret rotation fixture

```python
class TestSecretRotation:
    def test_queued_delivery_uses_new_secret_after_rotation(
        self, registry, worker, mock_http
    ):
        subscriber = registry.register(url="https://example.com/hook", secret="old_secret", min_score=70)
        # Queue a delivery
        worker.enqueue(subscriber_id=subscriber.id, payload=sample_payload())
        # Rotate secret before delivery runs
        registry.rotate_secret(subscriber.id, new_secret="new_secret")
        # Deliver — should use new_secret for signing
        worker.process_due()
        sent_sig = mock_http.last_request.headers["X-LedgerLens-Signature"]
        expected_sig = "sha256=" + hmac.new(
            b"new_secret", mock_http.last_request.content, hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(sent_sig, expected_sig)
```

### Dead-letter behaviour tests

```python
class TestDeadLetterBehaviour:
    def test_exactly_8_failures_triggers_dead_letter(self, worker, registry, mock_http):
        mock_http.side_effect = httpx.ConnectError("refused")
        subscriber = registry.register(url="https://fail.example.com/hook", secret="s", min_score=50)
        worker.enqueue(subscriber.id, sample_payload())
        for attempt in range(8):
            worker.process_due()
            advance_time_past_backoff(attempt)
        assert registry.get_subscriber(subscriber.id).status == "dead"

    def test_7_failures_does_not_dead_letter(self, worker, registry, mock_http):
        # 7 failures → still active
        ...

    def test_backoff_delays_are_exponential(self, worker, registry, mock_http):
        # Assert retry N is scheduled at now + 2^N * 5 seconds
        ...
```

### Static analysis test (constant-time comparison)

```python
import ast, pathlib

def test_hmac_comparison_is_constant_time():
    source = pathlib.Path("detection/webhook_worker.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            # Detect patterns like: computed_sig == received_sig
            # This is a heuristic; a proper check inspects operand names
            for op in node.ops:
                if isinstance(op, ast.Eq):
                    # Flag any == comparison involving variables named *sig* or *signature*
                    operands = [ast.unparse(node.left)] + [ast.unparse(c) for c in node.comparators]
                    for operand in operands:
                        assert "sig" not in operand.lower(), (
                            f"Possible timing-unsafe signature comparison at line {node.lineno}: "
                            f"{ast.unparse(node)}"
                        )
```

### SSRF protection tests

```python
@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1/hook",
    "https://192.168.1.1/hook",
    "https://10.0.0.1/hook",
    "https://172.16.0.1/hook",
    "http://[::1]/hook",
    "http://localhost/hook",
    "http://0.0.0.0/hook",
])
def test_private_ip_rejected_at_registration(self, client, bad_url):
    resp = client.post("/webhooks", json={"url": bad_url, "secret": "s", "min_score": 70})
    assert resp.status_code == 422
```

## Security Considerations

- Tests must use `hmac.compare_digest()` in all test assertions that compare HMAC values — tests themselves should not introduce timing vulnerabilities or bad patterns that developers might copy.
- Mock secrets in tests must be clearly labelled as test values (e.g., `"test_secret_do_not_use"`); no real-looking secrets or API keys in test fixtures.
- The AST-based static analysis test (`test_hmac_comparison_is_constant_time`) should be run as part of the standard `pytest` suite, not a separate lint step, so it is not accidentally skipped in CI.
- `freezegun` is used for all timestamp tests; do not rely on wall-clock comparisons that could make tests flaky in CI.
- Test fixtures must not make real outbound HTTP requests; use `respx` or `httpx.MockTransport`. Add a `pytest` fixture that asserts no real HTTP was made during the test run (via `respx` strict mode or equivalent).

## Testing Requirements

This issue *is* the testing requirement. The full acceptance criterion is:

- **≥95% branch coverage** on `detection/webhook_worker.py` and `detection/webhook_registry.py`.
- All 5 test classes (`TestHMACVerification`, `TestTimestampReplayPrevention`, `TestSecretRotation`, `TestDeadLetterBehaviour`, `TestConcurrency`) implemented with all sub-cases passing.
- `TestSSRFProtection` passes for all listed private IP patterns plus additional edge cases.
- Static analysis test (`test_hmac_comparison_is_constant_time`) passes.
- No flaky tests: all tests use deterministic mocks for time and HTTP.
- Test suite completes in <30 seconds on a standard developer machine.
- `pytest --tb=short` exits 0.

## Documentation Requirements

- Docstrings on all test classes explaining what security property is being verified.
- New file `docs/webhook_security_model.md` documenting: HMAC signing scheme, replay prevention window, secret rotation procedure, dead-letter recovery, and SSRF protection.
- Update `README.md` Testing section to note that `tests/test_webhook_security.py` covers the security model.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `tests/test_webhook_security.py` created with all 5 test classes and all parameterised sub-cases.
- [ ] `tests/test_webhook_worker.py` extended with dead-letter and backoff tests.
- [ ] Static analysis AST test for constant-time comparison passing.
- [ ] SSRF protection tests covering all listed private IP ranges.
- [ ] ≥95% branch coverage on `webhook_worker.py` and `webhook_registry.py`.
- [ ] All tests deterministic (frozen time, mocked HTTP).
- [ ] No real HTTP calls during test run.
- [ ] `docs/webhook_security_model.md` written.
- [ ] `README.md` and `CHANGELOG.md` updated.
- [ ] `pytest` exits 0 in <30 seconds.

## For Contributors

**Ideal contributor profile**: You have deep familiarity with webhook security — specifically HMAC-SHA256 signing, replay prevention, and constant-time comparison. You are experienced writing comprehensive Python test suites using `pytest` and mocking libraries (`respx`, `freezegun`, `unittest.mock`). Understanding of SSRF vulnerabilities and private IP range blocking is required. Experience with security-focused testing (timing attacks, replay attacks) in a production service context is highly valued.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., security testing, Python test engineering, webhook systems).
2. **Relevant experience**: webhook security test suites, HMAC replay prevention implementations, or SSRF protection work you have shipped.
3. **Approach / thoughts**: how you would structure the secret rotation test to guarantee atomicity — specifically, how you would test that no delivery is duplicated when a secret rotates while a worker is mid-flight.
4. **Estimated time**: your realistic estimate to complete all test classes, docs, and the Definition of Done checklist.
