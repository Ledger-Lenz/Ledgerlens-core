"""Order book event ingestion from Horizon operations.

Stellar's order book is implemented via `manage_buy_offer` /
`manage_sell_offer` operations rather than a dedicated order-book history
endpoint. This module maps those operations onto `OrderBookEvent` records
for `detection.feature_engineering`'s cancellation-rate/timing features.
"""

import httpx

from config.settings import settings
from ingestion.data_models import OrderBookEvent

OFFER_OPERATION_TYPES = ("manage_buy_offer", "manage_sell_offer", "create_passive_sell_offer")


def _event_type(record: dict) -> str:
    """Classify an offer operation as created/updated/cancelled.

    An offer operation with `amount == "0"` removes the offer (cancellation).
    `offer_id == "0"` (or absent) means a brand new offer was created;
    any other `offer_id` with a non-zero amount is an update to an
    existing offer.
    """
    if record.get("amount") == "0":
        return "cancelled"
    if record.get("offer_id") in (None, "0", 0):
        return "created"
    return "updated"


def _parse_event(record: dict) -> OrderBookEvent:
    selling = record.get("selling_asset_code", "XLM")
    buying = record.get("buying_asset_code", "XLM")
    side = "sell" if record["type"] != "manage_buy_offer" else "buy"
    price = float(record.get("price", 0.0))
    amount = float(record.get("amount", 0.0))

    return OrderBookEvent(
        id=record["id"],
        timestamp=record["created_at"],
        account=record["source_account"],
        asset_pair=f"{selling}/{buying}",
        side=side,
        amount=amount,
        price=price,
        event_type=_event_type(record),
    )


def load_order_book_events(account: str, limit: int = 200) -> list[OrderBookEvent]:
    """Fetch recent offer-related operations for `account` as `OrderBookEvent` records."""
    url = f"{settings.horizon_url}/accounts/{account}/operations"
    params = {"order": "desc", "limit": limit}

    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        records = response.json()["_embedded"]["records"]

    return [_parse_event(r) for r in records if r["type"] in OFFER_OPERATION_TYPES]
