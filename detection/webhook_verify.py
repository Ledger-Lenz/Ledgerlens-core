"""Reference HMAC verification for LedgerLens webhook consumers.

Every delivery carries two headers:

* ``X-LedgerLens-Signature: sha256=<hex>`` — HMAC-SHA256 of the raw request
  body, keyed with the subscriber secret returned at registration
  (see ``detection.webhook_registry.register_subscriber``).
* ``X-LedgerLens-Timestamp: <unix seconds>`` — send time, used to reject
  replays outside a tolerance window.

Consumers must verify against the *raw* body bytes (not re-serialized JSON)
and compare in constant time.  Minimal Python equivalent::

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(expected, request.headers["X-LedgerLens-Signature"])

CLI usage::

    python -m detection.webhook_verify --secret "$SECRET" \\
        --signature "sha256=..." [--timestamp 1700000000] < body.json
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time

SIGNATURE_HEADER = "X-LedgerLens-Signature"
TIMESTAMP_HEADER = "X-LedgerLens-Timestamp"
DEFAULT_TOLERANCE_SECONDS = 300


def compute_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time check that *signature* matches *body* under *secret*."""
    if not signature or not signature.startswith("sha256="):
        return False
    return hmac.compare_digest(compute_signature(body, secret), signature)


def verify_timestamp(
    timestamp: str | int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Return True if *timestamp* is within *tolerance_seconds* of *now*."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= tolerance_seconds


def verify_webhook(
    body: bytes,
    headers: dict[str, str],
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Verify both signature and freshness from a delivery's headers."""
    lowered = {k.lower(): v for k, v in headers.items()}
    signature = lowered.get(SIGNATURE_HEADER.lower(), "")
    timestamp = lowered.get(TIMESTAMP_HEADER.lower())
    return verify_signature(body, signature, secret) and verify_timestamp(
        timestamp, tolerance_seconds, now
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a LedgerLens webhook signature")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--timestamp", help="X-LedgerLens-Timestamp value to check")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE_SECONDS)
    args = parser.parse_args(argv)

    body = sys.stdin.buffer.read()
    ok = verify_signature(body, args.signature, args.secret)
    if ok and args.timestamp is not None:
        ok = verify_timestamp(args.timestamp, args.tolerance)
    print("valid" if ok else "invalid")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
