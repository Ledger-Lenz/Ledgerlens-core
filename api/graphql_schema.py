import secrets
import logging
from typing import Optional

import strawberry
from strawberry.types import Info

from config.settings import settings
from detection import storage
from detection.api_key_store import lookup_key
from api.cross_chain_router import get_links_for_wallet
from detection.model_registry import get_current_version

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
    def score(self, info: Info, asset_pair: Optional[str] = None) -> list[RiskScoreType]:
        _require_scope(info, "read:scores")
        try:
            scores = storage.get_latest_scores(self.address, asset_pair)
            return [RiskScoreType(**s.model_dump()) for s in scores]
        except Exception as exc:
            logger.error("Failed to fetch scores for wallet %s: %s", self.address, exc)
            return []

    @strawberry.field
    def shap_explanation(self, info: Info, model: str = "random_forest") -> ShapExplanationType:
        _require_scope(info, "read:scores")
        version = get_current_version(model, None) or "unknown"
        return ShapExplanationType(
            wallet=self.address, model_version=version,
            base_value=0.0, contributions=[], summary_sentence="", model_name=model,
        )

    @strawberry.field
    def cross_chain_links(self, info: Info) -> list[CrossChainLinkType]:
        _require_admin(info)
        try:
            links = get_links_for_wallet(self.address)
            return [CrossChainLinkType(chain=link["chain"], evm_wallet=link["evm_wallet"], confidence=link["confidence"]) for link in links]
        except Exception as exc:
            logger.error("Failed to fetch cross-chain links for wallet %s: %s", self.address, exc)
            return []


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
        logger.warning("GraphQL auth: no request context")
        raise strawberry.GraphQLError("Unauthorized: no request context")
    api_key = request.headers.get("X-LedgerLens-Api-Key") or request.headers.get("X-LedgerLens-Admin-Key")
    if not api_key:
        logger.warning("GraphQL auth: missing API key")
        raise strawberry.GraphQLError("Unauthorized: missing API key")
    # Validate the key against the store
    key_meta = lookup_key(api_key)
    if key_meta is None:
        logger.warning("GraphQL auth: invalid API key")
        raise strawberry.GraphQLError("Unauthorized: invalid or revoked API key")
    # Admin keys satisfy any scope
    scopes = set(key_meta["scopes"].split(",")) if key_meta.get("scopes") else set()
    if scope not in scopes and "admin" not in scopes:
        logger.warning("GraphQL auth: key lacks required scope '%s' (has: %s)", scope, scopes)
        raise strawberry.GraphQLError(f"Forbidden: this field requires the '{scope}' scope")


def _require_admin(info: Info) -> None:
    request = info.context.get("request")
    if request is None:
        logger.warning("GraphQL admin auth: no request context")
        raise strawberry.GraphQLError("Unauthorized: no request context")
    admin_key = request.headers.get("X-LedgerLens-Admin-Key")
    if not admin_key:
        logger.warning("GraphQL admin auth: missing admin key")
        raise strawberry.GraphQLError("Unauthorized: missing admin key")
    # Check against configured admin API key
    if not settings.admin_api_key or not secrets.compare_digest(admin_key, settings.admin_api_key):
        logger.warning("GraphQL admin auth: invalid admin key")
        raise strawberry.GraphQLError("Unauthorized: invalid admin key")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

schema = strawberry.Schema(query=Query)
