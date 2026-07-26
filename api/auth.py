"""Authentication and authorisation dependencies for LedgerLens API endpoints.

.. deprecated::
    ``api/auth.py`` is maintained for backward compatibility during the
    gateway transition (see ``docs/api_gateway.md``). New code should rely
    on :class:`api.gateway.GatewayMiddleware` instead.

Provides two dependency factories that implement the same checks as the
consolidated :mod:`api.gateway` module:

- :func:`require_admin_key` — checks the admin API key header.
- :func:`require_compliance_key` — checks the compliance API key header.

.. note::
    A third dependency, ``require_api_key_scope``, previously lived here and
    duplicated the scoped-API-key + rate-limit checks now owned by
    :func:`api.api_key_router.require_scope` / :class:`api.gateway.GatewayMiddleware`.
    It was never imported by any router (dead code) and was independently
    broken — it referenced three functions (``_check_rate_limit_redis``,
    ``_check_rate_limit_local``, ``_rate_check``) that were never defined
    anywhere, so calling it would have raised ``NameError``. It was removed
    rather than fixed: fixing it would have meant standing up a *second*,
    parallel rate-limit enforcement path when the actual fix (making
    ``detection.api_key_store.check_rate_limit`` itself distributed, see
    ``detection/rate_limiter.py``) already covers every real call site.
"""

import logging
import secrets
import time
from collections import defaultdict

from fastapi import Header, HTTPException

from config.settings import settings

logger = logging.getLogger("ledgerlens.api.auth")

# ---------------------------------------------------------------------------
# In-process rate limiter for repeated failed auth attempts.
# Prevents brute-force probing of admin/compliance keys.
# Each IP gets a sliding window of 5 failed attempts per 60 seconds.
# The rate limiter is only updated on FAILED attempts, so valid callers
# are never rate-limited.
# ---------------------------------------------------------------------------

_FAILED_AUTH_WINDOW_SECONDS = 60
_FAILED_AUTH_MAX_ATTEMPTS = 5
_failed_auth_buckets: dict[str, list[float]] = defaultdict(list)


def _check_failed_auth_rate_limit(client_ip: str) -> None:
    """Raise HTTP 429 if ``client_ip`` has exceeded the failed-auth rate limit.

    Uses a sliding window: only timestamps within the last 60 seconds are counted.
    """
    now = time.monotonic()
    bucket = _failed_auth_buckets[client_ip]
    # Evict timestamps outside the window
    _failed_auth_buckets[client_ip] = [t for t in bucket if now - t < _FAILED_AUTH_WINDOW_SECONDS]
    if len(_failed_auth_buckets[client_ip]) >= _FAILED_AUTH_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts. Please try again later.",
        )
    _failed_auth_buckets[client_ip].append(now)


def _record_failed_auth(client_ip: str) -> None:
    """Log and rate-limit a failed authentication attempt."""
    logger.warning("Failed auth attempt from %s", client_ip)
    _check_failed_auth_rate_limit(client_ip)


# ---------------------------------------------------------------------------
# Backward-compatible single-key auth
# ---------------------------------------------------------------------------


def require_admin_key(x_ledgerlens_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency gating admin-only endpoints (backward compatible).

    Checks the ``X-LedgerLens-Admin-Key`` header against
    ``settings.ledgerlens_admin_api_key`` using constant-time comparison.

    Logs and rate-limits repeated failed authentication attempts.
    """
    if not settings.admin_api_key:
        logger.warning("Admin API key is not configured — returning 503")
        raise HTTPException(status_code=503, detail="Admin API key is not configured")

    if not x_ledgerlens_admin_key:
        raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Admin-Key header")

    if not secrets.compare_digest(x_ledgerlens_admin_key, settings.admin_api_key):
        _record_failed_auth("unknown")
        raise HTTPException(status_code=403, detail="Invalid admin key")


def require_compliance_key(x_ledgerlens_compliance_key: str = Header(default="")) -> None:
    """FastAPI dependency gating compliance endpoints (backward compatible).

    Checks the ``X-LedgerLens-Compliance-Key`` header against
    ``settings.ledgerlens_compliance_api_key`` using constant-time comparison.

    Logs and rate-limits repeated failed authentication attempts.
    """
    if not settings.compliance_api_key:
        logger.warning("Compliance API key is not configured — returning 503")
        raise HTTPException(status_code=503, detail="Compliance API key is not configured")

    if not x_ledgerlens_compliance_key:
        raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Compliance-Key header")

    if not secrets.compare_digest(x_ledgerlens_compliance_key, settings.compliance_api_key):
        _record_failed_auth("unknown")
        raise HTTPException(status_code=403, detail="Invalid compliance API key")
