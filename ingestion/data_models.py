"""Pydantic schemas for Stellar DEX trade and order book records.

These models are the shared "shape" of trade data as it flows from
ingestion -> detection. The ledgerlens-data repo persists records in
this shape; the ledgerlens-api repo serializes RiskScore (see
detection/risk_score.py) using the same field names so consumers across
the org stay in sync. See README.md's "LedgerLens Organization" section
for the cross-repo data contract.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Asset(BaseModel):
    code: str
    issuer: str | None = None  # None for native XLM

    @property
    def is_native(self) -> bool:
        return self.issuer is None

    @property
    def pair_symbol(self) -> str:
        return self.code if self.is_native else f"{self.code}:{self.issuer}"


class Trade(BaseModel):
    """A single executed trade on the SDEX."""

    id: str
    ledger_close_time: datetime
    base_account: str
    counter_account: str
    base_asset: Asset
    counter_asset: Asset
    base_amount: float
    counter_amount: float
    price: float
    base_is_seller: bool

    @property
    def asset_pair(self) -> str:
        return f"{self.base_asset.pair_symbol}/{self.counter_asset.pair_symbol}"


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
