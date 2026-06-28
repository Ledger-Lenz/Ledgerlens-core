"""Wallet allowlist and denylist management API — Issue #181.

Provides CRUD endpoints so exchange operators can permanently flag wallets as
trusted (allowlisted) or confirmed bad actors (denylisted) independently of
the ML risk score.  An active override takes effect immediately on the next
``GET /v1/scores/{wallet}`` call.

All mutations are admin-key gated.  The underlying ``wallet_overrides`` table
uses soft deletes so the full audit trail (who added or removed each entry and
when) is always preserved.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import require_admin_key
from detection.storage import (
    add_wallet_override,
    get_wallet_overrides,
    remove_wallet_override,
)

router = APIRouter(
    prefix="/admin",
    tags=["Allowlist / Denylist"],
    dependencies=[Depends(require_admin_key)],
)


class OverrideCreate(BaseModel):
    wallet: str
    reason: str
    added_by: str


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@router.post(
    "/allowlist",
    status_code=201,
    summary="Add a wallet to the allowlist",
    description=(
        "Marks a wallet as trusted.  Overrides the computed risk score to 0 "
        "with ``override: 'allowlisted'`` on ``GET /v1/scores/{wallet}``. "
        "Requires ``X-LedgerLens-Admin-Key`` header."
    ),
)
def add_to_allowlist(body: OverrideCreate) -> dict:
    entry = add_wallet_override(
        wallet=body.wallet,
        list_type="allowlist",
        reason=body.reason,
        added_by=body.added_by,
    )
    return entry


@router.get(
    "/allowlist",
    summary="List all allowlisted wallets",
    description=(
        "Returns active allowlist entries with pagination.  Pass "
        "``include_removed=true`` to include soft-deleted entries in the audit trail."
    ),
)
def list_allowlist(
    include_removed: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    return get_wallet_overrides(
        list_type="allowlist",
        include_removed=include_removed,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/allowlist/{wallet}",
    summary="Remove a wallet from the allowlist",
    description=(
        "Soft-deletes the active allowlist entry for ``wallet``.  The removal "
        "is recorded in the audit trail with ``removed_at`` timestamp. "
        "Returns 404 if the wallet has no active allowlist entry."
    ),
)
def remove_from_allowlist(wallet: str, removed_by: str = Query(...)) -> dict:
    removed = remove_wallet_override(
        wallet=wallet,
        list_type="allowlist",
        removed_by=removed_by,
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No active allowlist entry found for wallet {wallet}",
        )
    return {"status": "removed", "wallet": wallet, "removed_by": removed_by}


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------


@router.post(
    "/denylist",
    status_code=201,
    summary="Add a wallet to the denylist",
    description=(
        "Marks a wallet as a confirmed bad actor.  Overrides the computed risk "
        "score to 100 with ``override: 'denylisted'`` on ``GET /v1/scores/{wallet}``. "
        "Requires ``X-LedgerLens-Admin-Key`` header."
    ),
)
def add_to_denylist(body: OverrideCreate) -> dict:
    entry = add_wallet_override(
        wallet=body.wallet,
        list_type="denylist",
        reason=body.reason,
        added_by=body.added_by,
    )
    return entry


@router.get(
    "/denylist",
    summary="List all denylisted wallets",
    description=(
        "Returns active denylist entries with pagination.  Pass "
        "``include_removed=true`` to include soft-deleted entries in the audit trail."
    ),
)
def list_denylist(
    include_removed: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    return get_wallet_overrides(
        list_type="denylist",
        include_removed=include_removed,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/denylist/{wallet}",
    summary="Remove a wallet from the denylist",
    description=(
        "Soft-deletes the active denylist entry for ``wallet``.  The removal "
        "is recorded in the audit trail with ``removed_at`` timestamp. "
        "Returns 404 if the wallet has no active denylist entry."
    ),
)
def remove_from_denylist(wallet: str, removed_by: str = Query(...)) -> dict:
    removed = remove_wallet_override(
        wallet=wallet,
        list_type="denylist",
        removed_by=removed_by,
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"No active denylist entry found for wallet {wallet}",
        )
    return {"status": "removed", "wallet": wallet, "removed_by": removed_by}
