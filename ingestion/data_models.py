"""Pydantic schemas for Stellar DEX trade and order book records.

These models are the shared "shape" of trade data as it flows from
ingestion -> detection. The ledgerlens-data repo persists records in
this shape; the ledgerlens-api repo serializes RiskScore (see
detection/risk_score.py) using the same field names so consumers across
the org stay in sync. See README.md's "LedgerLens Organization" section
for the cross-repo data contract.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)

StrictString = Annotated[str, Field(strict=True)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
PositiveInteger = Annotated[int, Field(gt=0, strict=True)]


class IngestionModel(BaseModel):
    """Base configuration shared by records crossing the ingestion boundary.

    Models remain non-strict by default because Horizon encodes timestamps and
    numeric values as strings. Security-sensitive identifiers opt into strict
    string validation field-by-field, and unknown future Horizon fields are
    ignored for forward compatibility.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        strict=False,
        extra="ignore",
    )


def _validated_float(value: Any, field_name: str) -> float:
    """Parse a finite Horizon numeric value without accepting booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number")
    return parsed


def _validated_decimal(value: Any, field_name: str) -> Decimal:
    """Parse an exact Horizon decimal value after rejecting unsafe inputs."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number")
    return parsed


class Asset(IngestionModel):
    """A native XLM asset or an issued Stellar credit asset."""

    code: StrictString
    issuer: StrictString | None = None  # None only for native XLM

    @field_validator("code", "issuer", mode="before")
    @classmethod
    def validate_string_fields(cls, value: Any) -> str | None:
        """Reject non-string identifiers and trim surrounding API whitespace."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("asset identifiers must be strings")
        value = value.strip()
        if not value:
            raise ValueError("asset identifiers must not be empty")
        return value

    @model_validator(mode="after")
    def validate_native_asset(self) -> "Asset":
        """Require an issuer for credit assets while allowing issuer-less XLM."""
        if self.code != "XLM" and self.issuer is None:
            raise ValueError("Non-native assets require an issuer")
        return self

    @property
    def is_native(self) -> bool:
        return self.issuer is None

    @property
    def pair_symbol(self) -> str:
        return self.code if self.is_native else f"{self.code}:{self.issuer}"


class TradeType(str, Enum):
    ORDERBOOK = "orderbook"
    LIQUIDITY_POOL = "liquidity_pool"


class Trade(IngestionModel):
    """A single executed trade on the SDEX, either order-book or AMM-pool.

    `counter_account` is `None` for liquidity-pool trades: the pool has no
    `AccountId` and can't sign, so it cannot be represented as a wallet
    without fabricating a counterparty (see `liquidity_pool_id`).
    """

    id: StrictString
    paging_token: StrictString | None = None
    ledger_close_time: datetime
    base_account: StrictString
    counter_account: StrictString | None = None
    base_asset: Asset
    counter_asset: Asset
    base_amount: PositiveFloat
    counter_amount: PositiveFloat
    price: PositiveFloat
    base_is_seller: bool
    trade_type: TradeType = TradeType.ORDERBOOK
    liquidity_pool_id: StrictString | None = None  # set for liquidity-pool trades
    transaction_hash: StrictString | None = None  # links a trade back to its parent tx
    path_payment_id: StrictString | None = None  # originating path payment operation ID
    hop_index: int | None = None         # position in the path (0 = first hop)
    source: str = "stellar"              # ingestion origin: "stellar" | "solana" | ...

    @field_validator("base_amount", "counter_amount", "price", mode="before")
    @classmethod
    def parse_numeric_fields(cls, value: Any, info: ValidationInfo) -> float:
        """Sanitise raw Horizon string numbers before constrained validation."""
        return _validated_float(value, info.field_name)

    @model_validator(mode="after")
    def validate_trade_relationships(self) -> "Trade":
        """Validate pool metadata and path-payment hop bounds."""
        if self.trade_type == TradeType.LIQUIDITY_POOL and not self.liquidity_pool_id:
            raise ValueError("Liquidity-pool trades require liquidity_pool_id")
        if self.hop_index is not None and self.hop_index < 0:
            raise ValueError("hop_index must be non-negative")
        return self

    @property
    def asset_pair(self) -> str:
        return f"{self.base_asset.pair_symbol}/{self.counter_asset.pair_symbol}"


class LiquidityPool(IngestionModel):
    """Current reserves and share count for a CAP-38 AMM liquidity pool."""

    id: StrictString
    fee_bp: int
    total_shares: NonNegativeFloat
    reserves: list[tuple[Asset, NonNegativeFloat]]


class PathPayment(IngestionModel):
    """An atomic `path_payment_strict_send`/`path_payment_strict_receive` operation."""

    id: StrictString
    transaction_hash: StrictString
    timestamp: datetime
    source_account: StrictString
    destination_account: StrictString
    source_asset: Asset
    destination_asset: Asset
    source_amount: PositiveFloat
    destination_amount: PositiveFloat
    path: list[Asset]  # intermediate hop assets; Horizon caps this at 5
    strict_send: bool  # True = path_payment_strict_send, False = strict_receive

    @field_validator("source_amount", "destination_amount", mode="before")
    @classmethod
    def parse_amounts(cls, value: Any, info: ValidationInfo) -> float:
        """Sanitise raw path-payment amounts before positivity checks."""
        return _validated_float(value, info.field_name)


class PathPaymentOperation(IngestionModel):
    """Horizon path payment operation record used by PathPaymentDecomposer."""

    id: StrictString
    paging_token: StrictString
    transaction_hash: StrictString
    ledger_close_time: datetime
    source_account: StrictString
    destination_account: StrictString
    source_asset: Asset
    destination_asset: Asset
    source_amount: PositiveDecimal
    destination_amount: PositiveDecimal
    path: list[Asset]  # intermediate assets; empty = direct swap
    operation_type: Literal["path_payment_strict_send", "path_payment_strict_receive"]

    @field_validator("source_amount", "destination_amount", mode="before")
    @classmethod
    def parse_amounts(cls, value: Any, info: ValidationInfo) -> Decimal:
        """Preserve exact Horizon decimal amounts, including scientific notation."""
        return _validated_decimal(value, info.field_name)


class TradeEffect(IngestionModel):
    """A single trade effect record from Horizon /effects?type=trade."""

    id: StrictString
    account: StrictString
    sold_asset_type: StrictString
    sold_asset_code: StrictString | None = None
    sold_asset_issuer: StrictString | None = None
    sold_amount: PositiveDecimal
    bought_asset_type: StrictString
    bought_asset_code: StrictString | None = None
    bought_asset_issuer: StrictString | None = None
    bought_amount: PositiveDecimal

    @field_validator("sold_amount", "bought_amount", mode="before")
    @classmethod
    def parse_amounts(cls, value: Any, info: ValidationInfo) -> Decimal:
        """Preserve exact trade-effect decimal amounts before validation."""
        return _validated_decimal(value, info.field_name)

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


class OrderBookEvent(IngestionModel):
    """An order placement, update, or cancellation event."""

    id: StrictString
    timestamp: datetime
    account: StrictString
    asset_pair: StrictString
    side: Literal["buy", "sell"]
    amount: NonNegativeFloat
    price: PositiveFloat
    event_type: Literal["created", "updated", "cancelled"]
    # Horizon uses zero as a create sentinel, not as a persistent offer ID.
    # Exclusion preserves the established serialized OrderBookEvent contract.
    offer_id: PositiveInteger | None = Field(default=None, exclude=True)

    @field_validator("amount", "price", mode="before")
    @classmethod
    def parse_numeric_fields(cls, value: Any, info: ValidationInfo) -> float:
        """Sanitise raw offer numbers and reject booleans/NaN/infinity."""
        return _validated_float(value, info.field_name)


class BridgeTransfer(IngestionModel):
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
