"""API key management with scoped permissions and per-key rate limits — Issue #195.

Implements production-grade access control replacing the single
``LEDGERLENS_API_KEY`` environment variable:

- ``POST /admin/api-keys``        — create a key; plaintext returned once.
- ``GET  /admin/api-keys``        — list active keys (no hashes exposed).
- ``DELETE /admin/api-keys/{id}`` — revoke a key immediately.

Keys are validated on each request via :func:`api.auth.require_api_key_scope`.
The plaintext key is hashed with BLAKE2b (256-bit) before storage; the hash
is never exposed after creation.

Rate limiting uses a Redis sliding-window counter when Redis is reachable and
falls back to an in-process TTL dict when it is not.
"""

import hashlib
import secrets
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import require_admin_key
from detection.storage import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
)

logger = logging.getLogger("ledgerlens.api_key_router")

router = APIRouter(
    prefix="/admin",
    tags=["API Key Management"],
    dependencies=[Depends(require_admin_key)],
)

_VALID_SCOPES = frozenset(["read:scores", "write:suppressions", "admin"])


class ApiKeyCreate(BaseModel):
    scopes: list[str]
    created_by: str
    namespace_id: str | None = None
    rate_limit_per_minute: int = 60
    expires_at: str | None = None


@router.post(
    "/api-keys",
    status_code=201,
    summary="Create a new scoped API key",
    description=(
        "Generates a new API key.  The plaintext key is returned **once** in "
        "``plaintext_key`` and is never stored.  All subsequent storage uses the "
        "BLAKE2b hash.  Valid scopes: ``read:scores``, ``write:suppressions``, ``admin``."
    ),
)
def create_key(body: ApiKeyCreate) -> dict:
    invalid = [s for s in body.scopes if s not in _VALID_SCOPES]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scopes: {invalid}. Valid scopes: {sorted(_VALID_SCOPES)}",
        )
    if not body.scopes:
        raise HTTPException(status_code=422, detail="At least one scope is required")

    if body.expires_at is not None:
        try:
            datetime.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="expires_at must be a valid ISO-8601 datetime string",
            )

    plaintext = f"llk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.blake2b(plaintext.encode(), digest_size=32).hexdigest()
    key_id = secrets.token_hex(16)

    record = create_api_key(
        key_id=key_id,
        key_hash=key_hash,
        scopes=body.scopes,
        created_by=body.created_by,
        namespace_id=body.namespace_id,
        rate_limit_per_minute=body.rate_limit_per_minute,
        expires_at=body.expires_at,
    )
    record["plaintext_key"] = plaintext
    return record


@router.get(
    "/api-keys",
    summary="List active API keys",
    description=(
        "Returns all non-revoked API key records.  Key hashes are **not** "
        "included in the response."
    ),
)
def get_keys() -> list[dict]:
    return list_api_keys()


@router.delete(
    "/api-keys/{key_id}",
    summary="Revoke an API key",
    description=(
        "Immediately revokes the key identified by ``key_id``.  Any subsequent "
        "request using the revoked key returns 401 with no caching delay."
    ),
)
def delete_key(key_id: str) -> dict:
    found = revoke_api_key(key_id)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"API key '{key_id}' not found or already revoked",
        )
    return {"status": "revoked", "key_id": key_id}
