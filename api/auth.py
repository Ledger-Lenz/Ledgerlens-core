"""Authentication and authorisation dependencies for LedgerLens API endpoints.

Provides three dependency factories:

- :func:`require_admin_key`       — original single admin key (backward-compat).
- :func:`require_compliance_key`  — compliance-scoped key for SAR/IVMS endpoints.
- :func:`require_api_key_scope`   — new per-key scoped auth (#195).  Accepts
                                    ``X-LedgerLens-Api-Key`` header; validates
                                    scope, expiry, revocation, and per-key rate
                                    limits via a Redis sliding-window counter with
                                    in-process fallback.
"""

import hashlib
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Header, HTTPException, Request

from config.settings import settings

logger = logging.getLogger("ledgerlens.auth")

# ---------------------------------------------------------------------------
# Existing single-key auth (backward compatible)
# ---------------------------------------------------------------------------


def require_admin_key(x_ledgerlens_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency gating admin-only endpoints (e.g. model observability).

    Fails closed: if no admin key is configured at all, every request is
    rejected with 503 rather than treating an unconfigured key as "auth
    disabled". Returns 401 if the header is missing, 403 if it doesn't match.
    Comparison is timing-safe (`secrets.compare_digest`), not `==`.
    """
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin API key is not configured")

    if not x_ledgerlens_admin_key:
        raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Admin-Key header")

    if not secrets.compare_digest(x_ledgerlens_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=403, detail="Invalid admin key")


def require_compliance_key(x_ledgerlens_compliance_key: str = Header(default="")) -> None:
    """FastAPI dependency gating the regulatory ``/compliance/`` endpoints.

    These endpoints emit FATF Travel-Rule / SAR evidence packages and so are
    held behind a dedicated ``compliance:read`` scope (a separate API key) rather
    than the general admin key, preventing accidental exposure of legally
    sensitive deliverables.

    Fails closed: if no compliance key is configured, every request is rejected
    with 503. Any request whose ``X-LedgerLens-Compliance-Key`` header is missing
    or does not match is rejected with 403 (i.e. lacks the ``compliance:read``
    scope). Comparison is timing-safe (`secrets.compare_digest`), not `==`.
    """
    if not settings.compliance_api_key:
        raise HTTPException(status_code=503, detail="Compliance API key is not configured")

    if not x_ledgerlens_compliance_key or not secrets.compare_digest(
        x_ledgerlens_compliance_key, settings.compliance_api_key
    ):
        raise HTTPException(status_code=403, detail="Missing or invalid compliance:read scope")


# ---------------------------------------------------------------------------
# Scoped API key auth — Issue #195
# ---------------------------------------------------------------------------

# In-process sliding-window fallback used when Redis is unavailable.
# Maps key_id → list of UNIX timestamps within the current window.
_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60.0  # seconds


def _hash_key(plaintext: str) -> str:
    return hashlib.blake2b(plaintext.encode(), digest_size=32).hexdigest()


def _check_rate_limit_redis(key_id: str, limit: int) -> tuple[bool, int]:
    """Attempt a Redis sliding-window check.  Returns (allowed, retry_after)."""
    try:
        import redis as _redis
        from config.settings import settings as _s

        r = _redis.from_url(_s.redis_url, socket_connect_timeout=1, socket_timeout=1)
        window_key = f"ratelimit:{key_id}"
        now = time.time()
        pipe = r.pipeline()
        pipe.zremrangebyscore(window_key, "-inf", now - _RATE_WINDOW)
        pipe.zadd(window_key, {str(now): now})
        pipe.zcard(window_key)
        pipe.expire(window_key, int(_RATE_WINDOW) + 1)
        results = pipe.execute()
        count = results[2]
        if count > limit:
            return False, int(_RATE_WINDOW - (now % _RATE_WINDOW)) + 1
        return True, 0
    except Exception:
        return None, 0  # type: ignore[return-value]


def _check_rate_limit_local(key_id: str, limit: int) -> tuple[bool, int]:
    """In-process sliding-window fallback."""
    now = time.monotonic()
    bucket = _rate_limit_buckets[key_id]
    _rate_limit_buckets[key_id] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(_rate_limit_buckets[key_id]) >= limit:
        oldest = min(_rate_limit_buckets[key_id])
        retry_after = max(1, int(_RATE_WINDOW - (now - oldest)) + 1)
        return False, retry_after
    _rate_limit_buckets[key_id].append(now)
    return True, 0


def require_api_key_scope(required_scope: str):
    """Return a FastAPI dependency that validates ``X-LedgerLens-Api-Key``.

    The dependency:
    1. Hashes the provided key with BLAKE2b-256.
    2. Looks up the hash in the ``api_keys`` table.
    3. Checks revocation status → 401 if revoked.
    4. Checks expiry → 401 if expired.
    5. Checks scope → 403 if ``required_scope`` not in key's scopes.
    6. Enforces per-key rate limit (Redis sliding window, local fallback) → 429.
    7. Updates ``last_used_at`` asynchronously.

    A missing header returns 401 so clients know authentication is required.
    """

    async def _dependency(
        request: Request,
        x_ledgerlens_api_key: str = Header(default=""),
    ) -> None:
        if not x_ledgerlens_api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing X-LedgerLens-Api-Key header",
            )

        from detection.storage import get_api_key_by_hash, touch_api_key_last_used

        key_hash = _hash_key(x_ledgerlens_api_key)
        record = get_api_key_by_hash(key_hash)

        if record is None:
            raise HTTPException(status_code=401, detail="Invalid API key")

        if record["revoked_at"] is not None:
            raise HTTPException(status_code=401, detail="API key has been revoked")

        if record["expires_at"] is not None:
            try:
                exp = datetime.fromisoformat(record["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    raise HTTPException(status_code=401, detail="API key has expired")
            except (ValueError, TypeError):
                pass

        if required_scope not in record["scopes"]:
            raise HTTPException(
                status_code=403,
                detail=f"API key lacks required scope '{required_scope}'",
            )

        limit = record["rate_limit_per_minute"]
        key_id = record["key_id"]

        allowed, retry_after = _check_rate_limit_redis(key_id, limit)
        if allowed is None:
            # Redis unavailable — use local fallback
            allowed, retry_after = _check_rate_limit_local(key_id, limit)

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limit} requests per minute",
                headers={"Retry-After": str(retry_after)},
            )

        try:
            touch_api_key_last_used(key_id)
        except Exception:
            pass

    return _dependency
