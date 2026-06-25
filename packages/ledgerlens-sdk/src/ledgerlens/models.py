"""Typed Pydantic v2 models for LedgerLens API requests and responses.

These intentionally mirror (but do not import from) `detection.risk_score`
and the response shapes in `api/main.py` in the main `ledgerlens-core`
repo -- this package is published standalone and must not depend on the
core detection engine. Keep the two in sync; see the main repo README's
"LedgerLens Organization" section for the cross-repo data contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RiskScore(BaseModel):
    """A single wallet/asset-pair risk score, as returned by the scoring pipeline."""

    wallet: str
    asset_pair: str
    score: int = Field(ge=0, le=100, description="0-100; higher = more suspicious")
    benford_flag: bool
    ml_flag: bool
    confidence: int = Field(ge=0, le=100)
    disputed: bool = False
    timestamp: datetime

    score_lower: float | None = None
    score_upper: float | None = None
    prediction_set: list[int] | None = None
    coverage_guarantee: float | None = None


class CrossChainLink(BaseModel):
    chain: str
    evm_wallet: str
    last_bridge_at: str


class WalletScoresResponse(BaseModel):
    """Response shape of `GET /scores/{wallet}`."""

    scores: list[RiskScore]
    cross_chain_links: list[CrossChainLink] = Field(default_factory=list)


class ShapContribution(BaseModel):
    """One entry in the `GET /scores/{wallet}/explain` response."""

    feature: str
    shap_value: float


class CounterfactualResult(BaseModel):
    rank: int
    distance: float
    predicted_score: float
    feature_deltas: dict[str, Any]
    human_readable: str


class CounterfactualResponse(BaseModel):
    """Response shape of `GET /scores/{wallet}/counterfactual`."""

    wallet: str
    asset_pair: str
    current_score: int
    target_score: int
    counterfactuals: list[CounterfactualResult]


class AssetRiskRanking(BaseModel):
    """One entry in the `GET /assets/risk-ranking` response."""

    asset_pair: str
    average_score: float
    wallet_count: int


class WebhookSubscriber(BaseModel):
    """One entry in the `GET /webhooks` response."""

    subscriber_id: str
    url: str
    secret: str  # masked by the server (e.g. "sk_***abcd")
    min_score: int
    wallet_filter: str | None = None
    asset_pair_filter: str | None = None
    created_at: str


class WebhookCreated(BaseModel):
    """Response of `POST /webhooks`."""

    subscriber_id: str


class Dispute(BaseModel):
    """Response shape of `GET /disputes/{dispute_id}` (voter identities
    hidden -- only approve/reject counts)."""

    dispute_id: str
    wallet: str
    asset_pair: str
    disputed_score: int | None = None
    soroban_tx_hash: str | None = None
    evidence_url: str | None = None
    submitted_at: str
    status: str
    votes: dict[str, int]
    resolved_at: str | None = None
    resolution: str | None = None


class DisputeCreated(BaseModel):
    """Response shape of `POST /disputes` -- the raw stored record,
    including unredacted `committee_votes` (empty at creation time)."""

    dispute_id: str
    wallet: str
    asset_pair: str
    disputed_score: int
    soroban_tx_hash: str
    evidence_url: str | None = None
    submitted_at: datetime
    status: str
    committee_votes: list[dict[str, Any]]
    resolved_at: datetime | None = None
    resolution: str | None = None


class HealthStatus(BaseModel):
    """Response shape of `GET /health`."""

    status: str
    db: str | None = None
    models: str | None = None
    circuits: dict[str, str] | None = None
