"""ledgerlens-sdk: a typed Python client for the LedgerLens wash-trading
detection API.

    from ledgerlens import LedgerLensClient

    client = LedgerLensClient(base_url="https://api.ledgerlens.io", api_key="...")
    result = client.get_score("GABC...")
"""

from .async_client import AsyncLedgerLensClient
from .client import LedgerLensClient
from .exceptions import LedgerLensAPIError, LedgerLensError
from .models import (
    AssetRiskRanking,
    CounterfactualResponse,
    CounterfactualResult,
    CrossChainLink,
    Dispute,
    DisputeCreated,
    HealthStatus,
    RiskScore,
    ShapContribution,
    WalletScoresResponse,
    WebhookCreated,
    WebhookSubscriber,
)

__version__ = "0.1.0"

__all__ = [
    "LedgerLensClient",
    "AsyncLedgerLensClient",
    "LedgerLensError",
    "LedgerLensAPIError",
    "RiskScore",
    "WalletScoresResponse",
    "CrossChainLink",
    "ShapContribution",
    "CounterfactualResponse",
    "CounterfactualResult",
    "AssetRiskRanking",
    "WebhookSubscriber",
    "WebhookCreated",
    "Dispute",
    "DisputeCreated",
    "HealthStatus",
    "__version__",
]
