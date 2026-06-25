"""Pydantic schemas for Stellar DEX trade and order book records.

These models are the shared "shape" of trade data as it flows from
ingestion -> detection. The ledgerlens-data repo persists records in
this shape; the ledgerlens-api repo serializes RiskScore (see
detection/risk_score.py) using the same field names so consumers across
the org stay in sync. See README.md's "LedgerLens Organization" section
for the cross-repo data contract.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr


class Asset(BaseModel):
    code: str
    issuer: str | None = None  # None for native XLM

    @property
    def is_native(self) -> bool:
        return self.issuer is None

    @property
    def pair_symbol(self) -> str:
        return self.code if self.is_native else f"{self.code}:{self.issuer}"


class TradeType(str, Enum):
    ORDERBOOK = "orderbook"
    LIQUIDITY_POOL = "liquidity_pool"


class Trade(BaseModel):
    """A single executed trade on the SDEX, either order-book or AMM-pool.

    `counter_account` is `None` for liquidity-pool trades: the pool has no
    `AccountId` and can't sign, so it cannot be represented as a wallet
    without fabricating a counterparty (see `liquidity_pool_id`).
    """

    id: str
    ledger_close_time: datetime
    base_account: str
    counter_account: str | None = None
    base_asset: Asset
    counter_asset: Asset
    base_amount: float
    counter_amount: float
    price: float
    base_is_seller: bool
    trade_type: TradeType = TradeType.ORDERBOOK
    liquidity_pool_id: str | None = None  # set when trade_type == LIQUIDITY_POOL
    transaction_hash: str | None = None  # links a trade back to its parent tx
    path_payment_id: str | None = None   # ID of the originating path payment operation
    hop_index: int | None = None         # position in the path (0 = first hop)

    @property
    def asset_pair(self) -> str:
        return f"{self.base_asset.pair_symbol}/{self.counter_asset.pair_symbol}"


class LiquidityPool(BaseModel):
    """Current reserves and share count for a CAP-38 AMM liquidity pool."""

    id: str
    fee_bp: int
    total_shares: float
    reserves: list[tuple[Asset, float]]


class PathPayment(BaseModel):
    """An atomic `path_payment_strict_send`/`path_payment_strict_receive` operation."""

    id: str
    transaction_hash: str
    timestamp: datetime
    source_account: str
    destination_account: str
    source_asset: Asset
    destination_asset: Asset
    source_amount: float
    destination_amount: float
    path: list[Asset]  # intermediate hop assets; Horizon caps this at 5
    strict_send: bool  # True = path_payment_strict_send, False = strict_receive


class PathPaymentOperation(BaseModel):
    """Horizon path payment operation record used by PathPaymentDecomposer."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    paging_token: str
    transaction_hash: str
    ledger_close_time: datetime
    source_account: str
    destination_account: str
    source_asset: Asset
    destination_asset: Asset
    source_amount: Decimal
    destination_amount: Decimal
    path: list[Asset]  # intermediate assets; empty = direct swap
    operation_type: Literal["path_payment_strict_send", "path_payment_strict_receive"]


class TradeEffect(BaseModel):
    """A single trade effect record from Horizon /effects?type=trade."""

    id: str
    account: str
    sold_asset_type: str
    sold_asset_code: str | None = None
    sold_asset_issuer: str | None = None
    sold_amount: Decimal
    bought_asset_type: str
    bought_asset_code: str | None = None
    bought_asset_issuer: str | None = None
    bought_amount: Decimal

    @property
    def sold_asset(self) -> Asset:
        if self.sold_asset_type == "native" or not self.sold_asset_code:
            return Asset(code="XLM", issuer=None)
        return Asset(code=self.sold_asset_code, issuer=self.sold_asset_issuer)

    @property
    def bought_asset(self) -> Asset:
        if self.bought_asset_type == "native" or not self.bought_asset_code:
            return Asset(code="XLM", issuer=None)
        return Asset(code=self.bought_asset_code, issuer=self.bought_asset_issuer)


class OrderBookEvent(BaseModel):
    """An order placement, update, or cancellation event."""

    id: str
    timestamp: datetime
    account: str
    asset_pair: str
    side: str  # "buy" | "sell"
    amount: float
    price: float
    event_type: str  # "created" | "updated" | "cancelled"


class BridgeTransfer(BaseModel):
    """A cross-chain bridge transfer linking a Stellar wallet to an EVM wallet."""

    chain: str
    direction: str  # "stellar_to_evm" | "evm_to_stellar"
    evm_wallet: str  # EIP-55 checksummed
    stellar_wallet: str  # G... format
    amount_usd: float | None = None
    token: str
    tx_hash_evm: str
    tx_hash_stellar: str | None = None
    timestamp: datetime

    # Integrity verification fields (populated by BridgeEventVerifier)
    canonical_hash: str | None = None
    verification_status: str = "disabled"
    verified_at: datetime | None = None

    # Raw log fields used for receipt verification — stored as private attrs so
    # they are excluded from serialisation and the DB schema.
    _log_index: int = PrivateAttr(default=0)
    _topics: list = PrivateAttr(default_factory=list)
    _data: str = PrivateAttr(default="")
    _block_hash: str = PrivateAttr(default="")

    def model_post_init(self, __context: Any) -> None:
        # Private attributes are set via keyword after normal init via __init__ below.
        pass

    def __init__(self, **data: Any) -> None:
        log_index = data.pop("_log_index", 0)
        topics = data.pop("_topics", [])
        raw_data = data.pop("_data", "")
        block_hash = data.pop("_block_hash", "")
        super().__init__(**data)
        self._log_index = log_index
        self._topics = topics
        self._data = raw_data
        self._block_hash = block_hash
