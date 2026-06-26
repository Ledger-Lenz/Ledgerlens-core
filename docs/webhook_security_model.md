# Webhook Security Model

This document describes the security properties of the LedgerLens webhook system, covering HMAC signing, replay prevention, secret rotation, dead-letter recovery, and SSRF protection.

## HMAC Signing Scheme

Every webhook delivery is signed with **HMAC-SHA256** using the subscriber's secret key.

The signature is sent in the `X-LedgerLens-Signature` header:

```
X-LedgerLens-Signature: sha256=<hex-digest>
```

The digest is computed over the **raw request body bytes** (not the parsed JSON). Receivers must verify this signature before trusting the payload:

```python
import hmac, hashlib

def verify_ledgerlens_webhook(body: bytes, secret: str, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # MUST use compare_digest — never use == (timing side-channel)
    return hmac.compare_digest(signature, expected)
```

**Always use `hmac.compare_digest`**, never `==`, to avoid timing side-channel attacks.

## Replay Prevention Window

Each delivery includes a `X-LedgerLens-Timestamp` header containing the Unix epoch second when the delivery was attempted.

Receivers **should** reject payloads whose timestamp falls outside a ±5 minute (300 second) window:

```python
import time

def verify_timestamp(ts: int, window_seconds: int = 300) -> bool:
    now = int(time.time())
    return abs(now - ts) <= window_seconds
```

This prevents a valid signed payload from being replayed hours or days later.

## Secret Rotation Procedure

1. Register a **new** subscriber with the new secret via `POST /webhooks`.
2. Update your receiver to accept both old and new secrets (dual-verification) during the cutover.
3. Deactivate the old subscriber via `DELETE /webhooks/{old_subscriber_id}`.
4. Once all in-flight deliveries for the old subscriber have completed or dead-lettered, remove dual-verification from your receiver.

Deliveries enqueued against the new `subscriber_id` will always be signed with the new secret — there is no ambiguity once the old subscriber is deactivated.

## Dead-Letter Recovery

After **8 consecutive delivery failures**, an item moves to `dead` status. Inspect dead-lettered items via:

```bash
GET /webhooks/dead-letters
```

To recover, fix the underlying issue (endpoint unreachable, invalid response) and re-enqueue the affected payloads manually or through the dispute/governance workflow.

## SSRF Protection

Subscriber URLs are validated at registration time:

- Only `https://` scheme is accepted (HTTP is rejected).
- Hostnames are resolved via DNS; private/reserved IP ranges are rejected:
  - `127.x.x.x` / `localhost` / `::1` (loopback)
  - `10.x.x.x` (RFC 1918)
  - `172.16–31.x.x` (RFC 1918)
  - `192.168.x.x` (RFC 1918)
  - `fc00::/7` (IPv6 ULA)
  - `0.0.0.0`

This prevents the delivery worker from being used as a proxy to reach internal services (SSRF).

## Test Coverage

`tests/test_webhook_security.py` provides exhaustive coverage:

| Test class | What it verifies |
|------------|-----------------|
| `TestHMACVerification` | Correct/wrong secrets, tampered body, wrong prefix |
| `TestTimestampReplayPrevention` | All boundary conditions of the 5-minute window |
| `TestSecretRotation` | New secret used after rotation; no duplicates |
| `TestDeadLetterBehaviour` | Exactly 8 failures; exponential backoff formula |
| `TestConcurrency` | 10 parallel deliveries; slow subscriber isolation |
| `TestSSRFProtection` | All private IP ranges; HTTP scheme |
| Static analysis | `webhook_worker.py` uses `hmac.compare_digest` not `==` |
