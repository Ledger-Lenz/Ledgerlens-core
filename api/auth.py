"""Authentication dependency for admin-only endpoints in `api/main.py`."""

import secrets

from fastapi import Header, HTTPException

from config.settings import settings


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
