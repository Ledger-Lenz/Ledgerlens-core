"""Cross-chain link hypothesis API endpoints.

Exposes Bayesian cross-chain wallet link hypotheses for Stellar wallets.
GET /cross-chain/links/{stellar_wallet} — accepted hypotheses sorted by confidence.
GET /cross-chain/links/{stellar_wallet}/explain — evidence feature breakdown.
"""

import time
from collections import defaultdict
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings
from detection.cross_chain_linker import CrossChainLinker

router = APIRouter(prefix="/cross-chain", tags=["cross-chain"])

# Rate limiter: 30 requests per minute per IP
_RATE_LIMIT = 30
_RATE_WINDOW = 60.0
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[client_ip]
    _rate_buckets[client_ip] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(_rate_buckets[client_ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")
    _rate_buckets[client_ip].append(now)


class WalletLinkOut(BaseModel):
    stellar_wallet: str
    evm_wallet: str
    confidence: float
    link_status: str
    bridge_event_count: int
    created_at: str


class LinkExplanationOut(BaseModel):
    stellar_wallet: str
    evm_wallet: str
    confidence: float
    link_status: str
    evidence_features: dict[str, float]
    log_likelihood_ratio: float


@router.get("/links/{stellar_wallet}", response_model=List[WalletLinkOut])
async def get_cross_chain_links(
    stellar_wallet: str,
    request: Request,
    min_confidence: float = Query(0.7, ge=0.0, le=1.0),
):
    """Return accepted cross-chain link hypotheses for a Stellar wallet."""
    _check_rate_limit(request.client.host if request.client else "unknown")

    linker = CrossChainLinker(db_path=settings.db_path)
    links = linker.get_accepted_links(stellar_wallet, min_confidence=min_confidence)
    return [
        WalletLinkOut(
            stellar_wallet=h.stellar_wallet,
            evm_wallet=h.evm_wallet,
            confidence=h.confidence,
            link_status=h.link_status.value,
            bridge_event_count=h.bridge_event_count,
            created_at=h.created_at.isoformat(),
        )
        for h in links
    ]


@router.get(
    "/links/{stellar_wallet}/explain",
    response_model=List[LinkExplanationOut],
    dependencies=[Depends(require_admin_key)],
)
async def explain_cross_chain_links(stellar_wallet: str):
    """Return evidence feature breakdown for each hypothesis. Admin-key gated."""
    linker = CrossChainLinker(db_path=settings.db_path)
    links = linker.get_accepted_links(stellar_wallet)
    return [
        LinkExplanationOut(
            stellar_wallet=h.stellar_wallet,
            evm_wallet=h.evm_wallet,
            confidence=h.confidence,
            link_status=h.link_status.value,
            evidence_features=h.evidence_features,
            log_likelihood_ratio=h.log_likelihood_ratio,
        )
        for h in links
    ]
