"""
Audit Log API Endpoints  (Issue #297)
=======================================
Exposes the event-sourced scoring audit log over HTTP.

Endpoints
---------
GET /audit/wallet/{wallet}
    Full chronological scoring event history for a wallet.
GET /audit/wallet/{wallet}/verify
    Chain integrity verification result (valid / tampered / no_events).
GET /audit/summary
    Summary statistics: events in last 24h, unique wallets, integrity violations.

Security
--------
* All endpoints require the admin API key.
* Wallet addresses are validated against the Stellar address pattern.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings

logger = logging.getLogger("ledgerlens.api.audit")

router = APIRouter(prefix="/audit", tags=["Audit Log"])

_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{55}$")

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_store = None
_verifier = None


def _get_store():
    global _store
    if _store is None:
        from audit.scoring_events import ScoringEventStore

        db_path = getattr(settings, "ledgerlens_db_path", "ledgerlens.db")
        max_keys = getattr(settings, "audit_feature_snapshot_max_keys", 50)
        _store = ScoringEventStore(db_path=db_path, max_feature_keys=max_keys)
    return _store


def _get_verifier():
    global _verifier
    if _verifier is None:
        from audit.scoring_events import ChainHashVerifier

        _verifier = ChainHashVerifier(_get_store())
    return _verifier


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ScoringEventResponse(BaseModel):
    event_id: str
    wallet: str
    namespace_id: str
    score: int
    previous_score: Optional[int]
    model_version: str
    triggered_by: str
    actor_id: Optional[str]
    chain_hash: str
    occurred_at: str
    feature_snapshot_keys: list[str]  # keys only — values are admin-sensitive


class ChainVerificationResponse(BaseModel):
    wallet: str
    status: str
    total_events: int
    first_tampered_event_id: Optional[str]
    verified_at: str


class AuditSummaryResponse(BaseModel):
    events_last_24h: int
    unique_wallets_scored: int
    integrity_violations: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/wallet/{wallet}",
    response_model=list[ScoringEventResponse],
    summary="Get scoring event history for a wallet",
    description=(
        "Returns the full chronological scoring event history for the given "
        "wallet address. Requires admin API key."
    ),
)
async def get_wallet_audit_log(
    wallet: str,
    limit: int = Query(100, ge=1, le=1000),
    since: Optional[datetime] = Query(None),
    _: str = Depends(require_admin_key),
) -> list[ScoringEventResponse]:
    if not _STELLAR_ADDRESS_PATTERN.match(wallet):
        raise HTTPException(
            status_code=400,
            detail="Invalid Stellar wallet address format.",
        )
    store = _get_store()
    events = await store.replay(wallet, since=since, limit=limit)

    verify_on_read = getattr(settings, "audit_verify_on_read", False)
    if verify_on_read and events:
        verifier = _get_verifier()
        result = await verifier.verify(wallet)
        if result.status == "tampered":
            logger.warning(
                "Chain tampering detected for wallet %s at event %s on read.",
                wallet[:8],
                result.first_tampered_event_id,
            )

    return [
        ScoringEventResponse(
            event_id=e.event_id,
            wallet=e.wallet,
            namespace_id=e.namespace_id,
            score=e.score,
            previous_score=e.previous_score,
            model_version=e.model_version,
            triggered_by=e.triggered_by,
            actor_id=e.actor_id,
            chain_hash=e.chain_hash,
            occurred_at=e.occurred_at.isoformat(),
            feature_snapshot_keys=sorted(e.feature_snapshot.keys()),
        )
        for e in events
    ]


@router.get(
    "/wallet/{wallet}/verify",
    response_model=ChainVerificationResponse,
    summary="Verify the audit chain integrity for a wallet",
    description=(
        "Walks the scoring event chain for the wallet, recomputing each "
        "chain hash. Returns VALID, TAMPERED (with first failing event ID), "
        "or no_events."
    ),
)
async def verify_wallet_chain(
    wallet: str,
    _: str = Depends(require_admin_key),
) -> ChainVerificationResponse:
    if not _STELLAR_ADDRESS_PATTERN.match(wallet):
        raise HTTPException(
            status_code=400,
            detail="Invalid Stellar wallet address format.",
        )
    verifier = _get_verifier()
    result = await verifier.verify(wallet)
    return ChainVerificationResponse(**result.to_dict())


@router.get(
    "/summary",
    response_model=AuditSummaryResponse,
    summary="Get audit log summary statistics",
    description=(
        "Returns aggregate statistics for the audit log: events in the last "
        "24 hours, unique wallets scored, and integrity violations detected."
    ),
)
async def get_audit_summary(
    _: str = Depends(require_admin_key),
) -> AuditSummaryResponse:
    store = _get_store()
    stats = await store.summary()
    return AuditSummaryResponse(**stats)
