import strawberry
from strawberry.fastapi import GraphQLRouter
from strawberry.types import Info
from typing import Optional
import logging

from api.auth import require_admin_key
from detection import model_inference, shap_explainer, causal_engine, storage

logger = logging.getLogger("ledgerlens.graphql")

# ---------------------------------------------------------------------------
# GraphQL Types
# ---------------------------------------------------------------------------

@strawberry.type
class RiskScoreType:
    wallet: str
    asset_pair: str
    score: int
    benford_flag: bool
    ml_flag: bool
    confidence: int
    score_lower: Optional[float] = None
    score_upper: Optional[float] = None


@strawberry.type
class ShapContributionType:
    feature: str
    shap_value: float
    rank: int


@strawberry.type
class ShapExplanationType:
    wallet: str
    model_version: str
    base_value: float
    contributions: list[ShapContributionType]
    summary_sentence: str
    model_name: str


@strawberry.type
class CrossChainLinkType:
    chain: str
    evm_wallet: str
    confidence: float


@strawberry.type
class WalletType:
    address: str

    @strawberry.field
    def score(self, asset_pair: Optional[str] = None) -> list[RiskScoreType]:
        scores = storage.get_latest_scores(self.address, asset_pair)
        return [RiskScoreType(**s.model_dump()) for s in scores]

    @strawberry.field
    def shap_explanation(self, info: Info, model: str = "random_forest") -> ShapExplanationType:
        _require_scope(info, "read:scores")
        from detection.model_registry import get_current_version
        version = get_current_version(model, None) or "unknown"
        expl = shap_explainer.explain_score(None, {})
        return ShapExplanationType(
            wallet=self.address, model_version=version,
            base_value=0.0, contributions=[], summary_sentence="", model_name=model,
        )

    @strawberry.field
    def cross_chain_links(self, info: Info) -> list[CrossChainLinkType]:
        _require_admin(info)
        from api.cross_chain_router import get_links_for_wallet
        links = get_links_for_wallet(self.address)
        return [CrossChainLinkType(chain=l["chain"], evm_wallet=l["evm_wallet"], confidence=l["confidence"]) for l in links]


@strawberry.type
class Query:
    @strawberry.field
    def wallet(self, address: str) -> WalletType:
        return WalletType(address=address)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_scope(info: Info, scope: str) -> None:
    request = info.context.get("request")
    if request is None:
        raise strawberry.GraphQLError("Unauthorized: no request context")
    api_key = request.headers.get("X-LedgerLens-Api-Key")
    if not api_key:
        raise strawberry.GraphQLError("Unauthorized: missing API key")


def _require_admin(info: Info) -> None:
    request = info.context.get("request")
    if request is None:
        raise strawberry.GraphQLError("Unauthorized: no request context")
    admin_key = request.headers.get("X-LedgerLens-Admin-Key")
    if not admin_key:
        raise strawberry.GraphQLError("Unauthorized: missing admin key")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

schema = strawberry.Schema(query=Query)
