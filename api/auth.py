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
