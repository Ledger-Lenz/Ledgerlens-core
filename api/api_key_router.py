"""API key management endpoints and scope/rate-limit enforcement (Issue #195)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from api.auth import require_admin_key
from detection.api_key_store import (
    check_rate_limit,
    create_api_key,
    list_api_keys,
    lookup_key,
    revoke_api_key,
)

router = APIRouter(prefix="/admin/api-keys", tags=["API Keys"], dependencies=[Depends(require_admin_key)])


class ApiKeyCreate(BaseModel):
    scopes: list[str]
    namespace_id: str = ""
    rate_limit_per_minute: int = 60
    expires_at: Optional[str] = None


@router.post(
    "",
    status_code=201,
    summary="Create API key",
    description=(
        "Create a new scoped API key. The plaintext key is returned once and never stored. "
        "Valid scopes: read:scores, write:suppressions, admin."
    ),
)
def create_key(body: ApiKeyCreate) -> dict:
    try:
        return create_api_key(
            scopes=body.scopes,
            namespace_id=body.namespace_id,
            rate_limit_per_minute=body.rate_limit_per_minute,
            expires_at=body.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete(
    "/{key_id}",
    summary="Revoke API key",
    description="Revoke a key immediately. Any subsequent request using this key returns 401.",
)
def revoke_key(key_id: str) -> dict:
    if not revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail=f"API key {key_id} not found or already revoked")
    return {"key_id": key_id, "status": "revoked"}


@router.get(
    "",
    summary="List API keys",
    description="Return all API keys with metadata (key hashes are never returned).",
)
def get_keys() -> list[dict]:
    return list_api_keys()


def require_scope(required_scope: str):
    """FastAPI dependency factory: enforce that the request carries a key with the required scope."""

    def _dependency(
        x_ledgerlens_api_key: str = Header(default="", alias="X-LedgerLens-Api-Key"),
        x_ledgerlens_admin_key: str = Header(default="", alias="X-LedgerLens-Admin-Key"),
        request: Request = None,
    ) -> None:
        plaintext = x_ledgerlens_api_key or x_ledgerlens_admin_key
        if not plaintext:
            raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Api-Key header")

        key_meta = lookup_key(plaintext)
        if key_meta is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        scopes = set(key_meta["scopes"].split(",")) if key_meta["scopes"] else set()
        if required_scope not in scopes and "admin" not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"This endpoint requires the '{required_scope}' scope",
            )

        allowed, retry_after = check_rate_limit(key_meta["key_id"], key_meta["rate_limit_per_minute"])
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
