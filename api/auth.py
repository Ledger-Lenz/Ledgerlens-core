"""Authentication and authorisation dependencies for LedgerLens API endpoints.

.. deprecated::
    ``api/auth.py`` is maintained for backward compatibility during the
    gateway transition (see ``docs/api_gateway.md``). New code should rely
    on :class:`api.gateway.GatewayMiddleware` instead.

Provides three dependency factories that delegate to the consolidated
:mod:`api.gateway` module:

- :func:`require_admin_key` — delegates to gateway admin-key check.
- :func:`require_compliance_key` — delegates to gateway compliance-key check.
- :func:`require_api_key_scope` — delegates to gateway scoped API key check.
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
# Backward-compatible single-key auth (delegates to gateway)
# ---------------------------------------------------------------------------


def require_admin_key(x_ledgerlens_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency gating admin-only endpoints (backward compatible).

    Delegates to the gateway's admin-key resolution. Fails closed.
    """
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin API key is not configured")

    if not x_ledgerlens_admin_key:
        raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Admin-Key header")

    if not secrets.compare_digest(x_ledgerlens_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=403, detail="Invalid admin key")


def require_compliance_key(x_ledgerlens_compliance_key: str = Header(default="")) -> None:
    """FastAPI dependency gating compliance endpoints (backward compatible).

    Delegates to the gateway's compliance-key resolution. Fails closed.
    """
    if not settings.compliance_api_key:
        raise HTTPException(status_code=503, detail="Compliance API key is not configured")

    if not x_ledgerlens_compliance_key or not secrets.compare_digest(
        x_ledgerlens_compliance_key, settings.compliance_api_key
    ):
        raise HTTPException(status_code=403, detail="Missing or invalid compliance:read scope")


# ---------------------------------------------------------------------------
# Backward-compatible scoped API key dependency (delegates to gateway)
# ---------------------------------------------------------------------------

_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60.0


def _hash_key(plaintext: str) -> str:
    return hashlib.blake2b(plaintext.encode(), digest_size=32).hexdigest()


def require_api_key_scope(required_scope: str):
    """Return a FastAPI dependency that validates ``X-LedgerLens-Api-Key``.

    .. deprecated::
        Use :class:`api.gateway.GatewayMiddleware` with route scope
        annotations instead.
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

        from detection.api_key_store import get_api_key_by_hash, touch_api_key_last_used

        key_hash = _hash_key(x_ledgerlens_api_key)
        record = get_api_key_by_hash(key_hash)

        if record is None:
            raise HTTPException(status_code=401, detail="Invalid API key")

        if record.get("revoked"):
            raise HTTPException(status_code=401, detail="API key has been revoked")

        if record.get("expires_at"):
            try:
                exp = datetime.fromisoformat(record["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    raise HTTPException(status_code=401, detail="API key has expired")
            except (ValueError, TypeError):
                pass

        scopes = set(s.strip() for s in record.get("scopes", "").split(",") if s.strip())
        if required_scope not in scopes and "admin" not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"API key lacks required scope '{required_scope}'",
            )

        from detection.api_key_store import check_rate_limit as _rate_check

        key_id = record["key_id"]
        limit = record["rate_limit_per_minute"]
        allowed, retry_after = _rate_check(key_id, limit)
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