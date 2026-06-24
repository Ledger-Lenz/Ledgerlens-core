"""Real-time trade ingestion from the Stellar Horizon API via Server-Sent Events.

Streams the `/trades` endpoint and yields `Trade` objects as ledgers close.
Downstream, `run_pipeline.py` feeds these into `detection.feature_engineering`.

Includes token-bucket rate limiting, backpressure control, and adaptive rate
reduction on HTTP 429 responses.
"""

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Optional

import sseclient

from config.settings import settings
from ingestion.data_models import Asset, Trade, TradeType
from ingestion.rate_limiter import TokenBucket

logger = logging.getLogger("ledgerlens.ingestion")


class BackpressureController:
    """Monitors queue depth and pauses SSE consumption at high watermark."""

    def __init__(
        self,
        queue: asyncio.Queue,
        high_watermark: int = 1000,
        low_watermark: int = 500,
    ):
        self._queue = queue
        self._high = high_watermark
        self._low = low_watermark
        self._paused = False

    async def check_and_wait(self) -> None:
        current_size = self._queue.qsize()
        if current_size >= self._high and not self._paused:
            self._paused = True
            logger.warning(
                "Backpressure: downstream queue at %d items, pausing SSE consumption",
                current_size,
            )
        if self._paused:
            while self._queue.qsize() > self._low:
                await asyncio.sleep(0.1)
            self._paused = False
            logger.info("Backpressure released: queue drained to %d items", self._queue.qsize())

    @property
    def is_paused(self) -> bool:
        return self._paused


class AdaptiveRateController:
    """Halves rate on HTTP 429 and restores linearly over restore_seconds."""

    def __init__(
        self,
        bucket: TokenBucket,
        configured_rate: float,
        restore_seconds: float = 60.0,
    ):
        self._bucket = bucket
        self._configured_rate = configured_rate
        self._restore_seconds = restore_seconds
        self._last_429_at: Optional[float] = None

    @property
    def last_429_at(self) -> Optional[float]:
        return self._last_429_at

    def on_429(self) -> None:
        new_rate = self._bucket.current_rate / 2.0
        self._bucket.set_rate(new_rate)
        self._last_429_at = time.monotonic()
        logger.warning("Horizon HTTP 429: reducing rate to %.1f req/s", self._bucket.current_rate)

    def tick(self) -> None:
        if self._last_429_at is None:
            return
        elapsed = time.monotonic() - self._last_429_at
        if elapsed >= self._restore_seconds:
            self._bucket.set_rate(self._configured_rate)
            logger.info("Rate restored to %.1f req/s after 429 backoff", self._configured_rate)
            self._last_429_at = None
        else:
            step = (self._configured_rate - self._bucket.current_rate) * (1.0 / self._restore_seconds)
            new_rate = min(self._bucket.current_rate + step, self._configured_rate)
            self._bucket.set_rate(new_rate)


class HorizonStreamer:
    """Async Horizon SSE streamer with rate limiting and backpressure."""

    def __init__(
        self,
        queue: Optional[asyncio.Queue] = None,
        rate_limit: float = 50.0,
        bucket_capacity: Optional[float] = None,
        high_watermark: int = 1000,
        low_watermark: int = 500,
        restore_seconds: float = 60.0,
    ):
        self._queue = queue or asyncio.Queue()
        self._bucket = TokenBucket(rate=rate_limit, capacity=bucket_capacity)
        self._backpressure = BackpressureController(
            self._queue,
            high_watermark=high_watermark,
            low_watermark=low_watermark,
        )
        self._adaptive = AdaptiveRateController(
            self._bucket,
            configured_rate=rate_limit,
            restore_seconds=restore_seconds,
        )
        self._configured_rate = rate_limit

    @property
    def bucket(self) -> TokenBucket:
        return self._bucket

    @property
    def backpressure(self) -> BackpressureController:
        return self._backpressure

    @property
    def adaptive(self) -> AdaptiveRateController:
        return self._adaptive

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    def rate_limiter_status(self) -> dict:
        return {
            "configured_rate": self._configured_rate,
            "current_rate": self._bucket.current_rate,
            "bucket_level": self._bucket.bucket_level,
            "backpressure_active": self._backpressure.is_paused,
            "queue_size": self._queue.qsize(),
            "last_429_at": self._adaptive.last_429_at,
        }


def _parse_trade(record: dict) -> Trade:
    """Convert a raw Horizon `/trades` record into a `Trade` model."""
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
    """Yield `Trade` objects as they occur on the SDEX."""
    for trade, _ in stream_trades_with_cursor(cursor):
        yield trade


def stream_trades_with_cursor(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
    """Yield ``(Trade, cursor)`` tuples as trades occur on the SDEX."""
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
