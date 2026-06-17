"""Order book event ingestion from Horizon operations.

Stellar's order book is implemented via `manage_buy_offer`,
`manage_sell_offer`, and `create_passive_sell_offer` operations rather than a
dedicated order-book history endpoint. This module maps those operations onto
`OrderBookEvent` records for `detection.feature_engineering`'s
cancellation-rate/timing features.

Event type mapping rules mirror Horizon offer operation semantics:

- `amount == "0"` (or numeric zero) removes an offer and maps to
  `event_type="cancelled"`.
- A non-zero offer operation with `offer_id == "0"` creates a new offer and
  maps to `event_type="created"`.
- A non-zero offer operation with a non-zero `offer_id` updates an existing
  offer and maps to `event_type="updated"`.
"""

from datetime import datetime, timezone

import httpx

from config.settings import settings
from ingestion.data_models import OrderBookEvent
from ingestion.http_client import AsyncHorizonClient, get_with_retry

OFFER_OPERATION_TYPES = ("manage_buy_offer", "manage_sell_offer", "create_passive_sell_offer")
PAGE_LIMIT = 200


def _horizon_url(path: str) -> str:
    return f"{settings.horizon_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_price(record: dict) -> float:
    price = record.get("price")
    if isinstance(price, dict):
        try:
            return float(price["n"]) / float(price["d"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return 0.0
    return _parse_float(price)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif hasattr(value, "to_pydatetime"):
        timestamp = value.to_pydatetime()
    elif value:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError:
            timestamp = datetime.fromtimestamp(0, tz=timezone.utc)
    else:
        timestamp = datetime.fromtimestamp(0, tz=timezone.utc)

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _parse_since(since: datetime) -> datetime:
    return _parse_datetime(since)


def _normalize_asset_arg(asset: str | None) -> str:
    if asset in (None, "", "native", "XLM"):
        return "XLM"
    return asset


def _asset_symbol(record: dict, prefix: str) -> str:
    asset_type = record.get(f"{prefix}_asset_type")
    code = record.get(f"{prefix}_asset_code")
    issuer = record.get(f"{prefix}_asset_issuer")

    if asset_type == "native" or not code:
        return "XLM"
    return f"{code}:{issuer}" if issuer else str(code)


def _event_type(record: dict) -> str:
    """Classify an offer operation as created/updated/cancelled."""
    if _parse_float(record.get("amount")) == 0.0:
        return "cancelled"
    if _parse_float(record.get("offer_id")) == 0.0:
        return "created"
    return "updated"


def _is_offer_operation(record: dict) -> bool:
    return record.get("type") in OFFER_OPERATION_TYPES


def _parse_event(record: dict) -> OrderBookEvent:
    selling = _asset_symbol(record, "selling")
    buying = _asset_symbol(record, "buying")
    operation_type = record.get("type")
    side = "buy" if operation_type == "manage_buy_offer" else "sell"

    return OrderBookEvent(
        id=str(record.get("id") or record.get("paging_token") or ""),
        timestamp=_parse_datetime(record.get("created_at") or record.get("ledger_close_time")),
        account=str(record.get("source_account") or ""),
        asset_pair=f"{selling}/{buying}",
        side=side,
        amount=_parse_float(record.get("amount")),
        price=_parse_price(record),
        event_type=_event_type(record),
    )


def _matches_asset_pair(
    event: OrderBookEvent,
    base_asset: str | None,
    counter_asset: str | None,
) -> bool:
    expected = f"{_normalize_asset_arg(base_asset)}/{_normalize_asset_arg(counter_asset)}"
    reversed_pair = f"{_normalize_asset_arg(counter_asset)}/{_normalize_asset_arg(base_asset)}"
    return event.asset_pair in {expected, reversed_pair}


def load_order_book_events(account: str, limit: int = PAGE_LIMIT) -> list[OrderBookEvent]:
    """Fetch recent offer-related operations for `account` as `OrderBookEvent` records."""
    url = _horizon_url(f"/accounts/{account}/operations")
    params = {"order": "desc", "limit": limit}

    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params=params)
        records = response.json().get("_embedded", {}).get("records", [])

    return [_parse_event(r) for r in records if _is_offer_operation(r)]


def load_order_book_events_for_pair(
    base_asset: str | None,
    counter_asset: str | None,
    since: datetime,
    limit: int = PAGE_LIMIT,
) -> list[OrderBookEvent]:
    """Fetch offer operations for an asset pair since `since` as `OrderBookEvent` records."""
    cutoff = _parse_since(since)
    url = _horizon_url("/operations")
    params = {"order": "desc", "limit": limit}

    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params=params)
        records = response.json().get("_embedded", {}).get("records", [])

    events: list[OrderBookEvent] = []
    for record in records:
        if not _is_offer_operation(record):
            continue
        event = _parse_event(record)
        if event.timestamp >= cutoff and _matches_asset_pair(event, base_asset, counter_asset):
            events.append(event)
    return events


def _filter_order_book_records(
    records: list[dict],
    base_asset: str | None,
    counter_asset: str | None,
    cutoff: datetime,
) -> list[OrderBookEvent]:
    events: list[OrderBookEvent] = []
    for record in records:
        if not _is_offer_operation(record):
            continue
        event = _parse_event(record)
        if event.timestamp >= cutoff and _matches_asset_pair(event, base_asset, counter_asset):
            events.append(event)
    return events


async def async_load_order_book_events_for_pair(
    base_asset: str | None,
    counter_asset: str | None,
    since: datetime,
    client: AsyncHorizonClient,
    limit: int = PAGE_LIMIT,
) -> list[OrderBookEvent]:
    """Async variant of `load_order_book_events_for_pair` using `AsyncHorizonClient`."""
    cutoff = _parse_since(since)
    data = await client.get("/operations", params={"order": "desc", "limit": limit})
    records = data.get("_embedded", {}).get("records", [])
    return _filter_order_book_records(records, base_asset, counter_asset, cutoff)
