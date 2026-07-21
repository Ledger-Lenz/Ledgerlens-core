"""Authentication and authorisation dependencies for LedgerLens API endpoints.

.. deprecated::
    ``api/auth.py`` is maintained for backward compatibility during the
    gateway transition (see ``docs/api_gateway.md``). New code should rely
    on :class:`api.gateway.GatewayMiddleware` instead.

Provides two dependency factories that delegate to the consolidated
:mod:`api.gateway` module:

- :func:`require_admin_key` — delegates to gateway admin-key check.
- :func:`require_compliance_key` — delegates to gateway compliance-key check.

The gateway migration (see ``docs/api_gateway.md``) replaced the previous
per-router scoped API key dependency with ``api.gateway.GatewayMiddleware`` /
``api.gateway.scope_required``; there is no scoped-key dependency here to
keep in sync with that migration.
"""

import logging
import secrets

from fastapi import Header, HTTPException

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
