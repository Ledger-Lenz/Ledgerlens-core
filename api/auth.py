"""Authentication and authorisation dependencies for LedgerLens API endpoints.

.. deprecated::
    ``api/auth.py`` is maintained for backward compatibility during the
    gateway transition (see ``docs/api_gateway.md``). New code should rely
    on :class:`api.gateway.GatewayMiddleware` instead.

Provides two dependency factories that delegate to the consolidated
:mod:`api.gateway` module:

- :func:`require_admin_key` — delegates to gateway admin-key check.
- :func:`require_compliance_key` — delegates to gateway compliance-key check.

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

import secrets

from fastapi import Header, HTTPException

from config.settings import settings

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
