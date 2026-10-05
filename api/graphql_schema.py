import logging
from typing import Optional

# ---------------------------------------------------------------------------
# Optional dependency: strawberry-graphql  (pip install 'ledgerlens-core[graphql]')
# ---------------------------------------------------------------------------
try:
    import strawberry
    from strawberry.types import Info
    _HAS_STRAWBERRY = True
except ImportError as _strawberry_err:  # pragma: no cover
    raise ImportError(
        "'strawberry-graphql' is required by api/graphql_schema.py but is not installed.\n"
        "  Install the 'graphql' extra:  pip install 'ledgerlens-core[graphql]'\n"
        "  Or install directly:          pip install 'strawberry-graphql[fastapi]'"
    ) from _strawberry_err

from graphql import GraphQLError

from api import policy
from api.graphql_cost import QueryCostLimiter
from detection import storage
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

def _enforce(info: Info, scope: str) -> None:
    """Enforce auth via the shared policy layer (#969), once per request+scope."""
    request = info.context.get("request")
    if request is None:
        logger.warning("GraphQL auth: no request context")
        raise GraphQLError("Unauthorized: no request context")
    cache = info.context.setdefault("_policy_decisions", {})
    decision = cache.get(scope)
    if decision is None:
        decision = policy.enforce(
            scope,
            admin_key=request.headers.get("X-LedgerLens-Admin-Key", ""),
            api_key=request.headers.get("X-LedgerLens-Api-Key", ""),
        )
        cache[scope] = decision
    if decision.status == policy.UNAUTHENTICATED:
        logger.warning("GraphQL auth: missing or invalid credentials")
        raise GraphQLError("Unauthorized: missing, invalid or revoked API key")
    if decision.status == policy.FORBIDDEN:
        logger.warning("GraphQL auth: key lacks required scope '%s'", scope)
        raise GraphQLError(f"Forbidden: this field requires the '{scope}' scope")
    if decision.status == policy.RATE_LIMITED:
        raise GraphQLError("Rate limit exceeded")


def _require_scope(info: Info, scope: str) -> None:
    _enforce(info, scope)


def _require_admin(info: Info) -> None:
    _enforce(info, "admin")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

schema = strawberry.Schema(query=Query, extensions=[QueryCostLimiter])
