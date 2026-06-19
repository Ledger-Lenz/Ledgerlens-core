"""Real-time trade ingestion from the Stellar Horizon API via Server-Sent Events.

Streams the `/trades` endpoint and yields `Trade` objects as ledgers close.
Downstream, `run_pipeline.py` feeds these into `detection.feature_engineering`.
"""

from collections.abc import Iterator

import sseclient

from config.settings import settings
from ingestion.data_models import Asset, Trade, TradeType


def _parse_trade(record: dict) -> Trade:
    """Convert a raw Horizon `/trades` record into a `Trade` model.

    Horizon's `/trades` endpoint returns both order-book and AMM pool
    trades (CAP-38). A pool trade carries `trade_type="liquidity_pool"`
    and a `base_liquidity_pool_id`/`counter_liquidity_pool_id` in place of
    a counterparty account — that side maps to `counter_account=None` plus
    `liquidity_pool_id` rather than a fabricated wallet.
    """
    base_asset = Asset(
        code=record.get("base_asset_code", "XLM"),
        issuer=record.get("base_asset_issuer"),
    )
    counter_asset = Asset(
        code=record.get("counter_asset_code", "XLM"),
        issuer=record.get("counter_asset_issuer"),
    )
    is_pool_trade = record.get("trade_type") == "liquidity_pool"
    liquidity_pool_id = record.get("base_liquidity_pool_id") or record.get("counter_liquidity_pool_id")
    return Trade(
        id=record["id"],
        ledger_close_time=record["ledger_close_time"],
        base_account=record.get("base_account") or "",
        counter_account=record.get("counter_account"),
        base_asset=base_asset,
        counter_asset=counter_asset,
        base_amount=float(record["base_amount"]),
        counter_amount=float(record["counter_amount"]),
        price=float(record["price"]["n"]) / float(record["price"]["d"]),
        base_is_seller=record["base_is_seller"],
        trade_type=TradeType.LIQUIDITY_POOL if is_pool_trade else TradeType.ORDERBOOK,
        liquidity_pool_id=liquidity_pool_id,
    )


def stream_trades(cursor: str = "now") -> Iterator[Trade]:
    """Yield `Trade` objects as they occur on the SDEX.

    Parameters
    ----------
    cursor:
        Horizon paging token to resume from, or "now" to start streaming
        from the current ledger.
    """
    for trade, _ in stream_trades_with_cursor(cursor):
        yield trade


def stream_trades_with_cursor(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
    """Yield ``(Trade, cursor)`` tuples as trades occur on the SDEX.

    The second element is the SSE event ID (Horizon paging token) which can
    be persisted and passed back as ``cursor`` to resume from that point.
    """
    url = f"{settings.horizon_stream_url}/trades?cursor={cursor}"
    headers = {"Accept": "text/event-stream"}

    client = sseclient.SSEClient(url, headers=headers)
    for event in client:
        if not event.data:
            continue
        record = _decode_event(event.data)
        if record is not None:
            yield _parse_trade(record), event.id or cursor


def _decode_event(data: str) -> dict | None:
    """Decode a single SSE payload into a Horizon record, skipping heartbeats."""
    import json

    if data == '"hello"':
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    for trade in stream_trades():
        print(trade.model_dump())
