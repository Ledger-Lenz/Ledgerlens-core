"""Real-time trade ingestion from the Stellar Horizon API via Server-Sent Events.

Streams the `/trades` endpoint and yields `Trade` objects as ledgers close.
Downstream, `run_pipeline.py` feeds these into `detection.feature_engineering`.

Connection attempts are gated by `horizon_circuit`: after
`HORIZON_FAILURE_THRESHOLD` consecutive connection/stream failures, the
breaker opens and `stream_trades_with_cursor` raises `CircuitOpenError`
immediately instead of continuing to retry, so a sustained Horizon outage
fails fast rather than exhausting connection attempts. Callers that want to
keep polling across an outage should catch `CircuitOpenError` and retry
after a delay -- the breaker will allow exactly one probe connection once
`HORIZON_RECOVERY_TIMEOUT_SECONDS` has elapsed.
"""

import logging
import time
from collections.abc import Iterator

import sseclient

from config.settings import settings
from ingestion.data_models import Asset, Trade, TradeType
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

logger = logging.getLogger(__name__)

HORIZON_FAILURE_THRESHOLD = 5
HORIZON_RECOVERY_TIMEOUT_SECONDS = 60.0
# Delay between reconnect attempts while the circuit is still CLOSED, so a
# string of immediate failures doesn't itself become a connection storm.
_RECONNECT_BACKOFF_SECONDS = 1.0

horizon_circuit = CircuitBreaker(
    name="horizon",
    failure_threshold=HORIZON_FAILURE_THRESHOLD,
    recovery_timeout=HORIZON_RECOVERY_TIMEOUT_SECONDS,
)


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

    Reconnects automatically on a dropped connection while
    `horizon_circuit` is CLOSED or HALF_OPEN. Once the circuit is OPEN
    (`HORIZON_FAILURE_THRESHOLD` consecutive failures), raises
    `CircuitOpenError` immediately instead of attempting another
    connection.
    """
    headers = {"Accept": "text/event-stream"}

    while True:
        if not horizon_circuit.allow_request():
            raise CircuitOpenError(horizon_circuit.name)

        url = f"{settings.horizon_stream_url}/trades?cursor={cursor}"
        try:
            client = sseclient.SSEClient(url, headers=headers)
            for event in client:
                if not event.data:
                    continue
                record = _decode_event(event.data)
                if record is not None:
                    trade = _parse_trade(record)
                    cursor = event.id or cursor
                    horizon_circuit.record_success()
                    yield trade, cursor
            # The SSE stream ended without raising -- treat as a successful
            # connection that simply closed, not a failure.
            return
        except Exception:
            horizon_circuit.record_failure()
            if horizon_circuit.state is CircuitState.OPEN:
                raise CircuitOpenError(horizon_circuit.name)
            logger.warning(
                "horizon_streamer: connection failed, retrying in %.1fs (cursor=%s)",
                _RECONNECT_BACKOFF_SECONDS,
                cursor,
            )
            time.sleep(_RECONNECT_BACKOFF_SECONDS)


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
