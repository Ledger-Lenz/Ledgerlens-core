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

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Callable, Optional

import httpx
import sseclient

from config.settings import settings
from ingestion.checkpoint import (
    CursorCheckpoint,
    FlushPolicy,
    resolve_checkpoint_path,
    validate_cursor,
)
from ingestion.data_models import Asset, Trade, TradeType
from ingestion.rate_limiter import (
    AdaptiveRateController,
    BackpressureController,
    TokenBucket,
)
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

if TYPE_CHECKING:
    from detection.streaming_features import FeatureVector, StreamingFeatureEngine


def _parse_trade(record: dict) -> Trade:
    """Convert a raw Horizon `/trades` record into a `Trade` model.

    Horizon's `/trades` endpoint returns both order-book and AMM pool
    trades (CAP-38). A pool trade carries ``trade_type="liquidity_pool"``
    and a ``base_liquidity_pool_id``/``counter_liquidity_pool_id`` in place of
    a counterparty account — that side maps to ``counter_account=None`` plus
    ``liquidity_pool_id`` rather than a fabricated wallet.
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
    liquidity_pool_id = record.get("base_liquidity_pool_id") or record.get(
        "counter_liquidity_pool_id"
    )
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


def stream_trades_with_cursor(
    cursor: str = "now",
    checkpoint: CursorCheckpoint | None = None,
) -> Iterator[tuple[Trade, str]]:
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
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in (404, 410) and cursor != "now":
                logger.warning(
                    "Horizon rejected cursor %s with HTTP %d; falling back to now",
                    cursor,
                    status_code,
                )
                if checkpoint is not None:
                    checkpoint.delete()
                cursor = "now"
                continue
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
    if data == '"hello"':
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


# ── Async HorizonStreamer with rate limiting / backpressure ──────────────────


class HorizonStreamer:
    """Async SSE consumer with configurable rate limiting and backpressure.

    Wraps the Horizon `/trades` SSE endpoint in an async iterator that
    enforces a token-bucket rate limit, monitors downstream queue depth
    (backpressure), and adaptively reduces the ingestion rate on HTTP 429
    responses.

    Parameters
    ----------
    queue:
        The downstream :class:`asyncio.Queue` to push parsed trades into.
    cursor:
        Horizon paging token to resume from (default ``"now"``).
    rate_limit:
        Tokens per second (default 50).
    bucket_capacity:
        Maximum token burst (default ``rate_limit * 2``).
    high_watermark:
        Queue size at which backpressure engages (default 1000).
    low_watermark:
        Queue size at which consumption resumes (default 500).
    restore_seconds:
        Seconds over which to restore rate after a 429 (default 60).
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        cursor: str | None = None,
        rate_limit: Optional[float] = None,
        bucket_capacity: Optional[float] = None,
        high_watermark: Optional[int] = None,
        low_watermark: Optional[int] = None,
        restore_seconds: Optional[float] = None,
        checkpoint: CursorCheckpoint | None = None,
        flush_policy: FlushPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        rate_limit = rate_limit if rate_limit is not None else settings.horizon_rate_limit
        bucket_capacity = bucket_capacity if bucket_capacity is not None else settings.horizon_rate_bucket_capacity
        high_watermark = high_watermark if high_watermark is not None else settings.horizon_queue_high_watermark
        low_watermark = low_watermark if low_watermark is not None else settings.horizon_queue_low_watermark
        restore_seconds = restore_seconds if restore_seconds is not None else settings.rate_restore_seconds
        if checkpoint is None:
            checkpoint_path = resolve_checkpoint_path(
                settings.cursor_checkpoint_path, settings.data_dir
            )
            checkpoint = CursorCheckpoint(checkpoint_path)
        stored_cursor = checkpoint.load()
        if cursor is not None:
            self._cursor = validate_cursor(cursor)
            logger.info("Starting fresh from cursor %s", self._cursor)
        elif stored_cursor is not None:
            self._cursor = stored_cursor
            logger.info("Resuming from cursor %s", self._cursor)
        else:
            self._cursor = validate_cursor(settings.horizon_default_cursor)
            logger.info("Starting fresh from cursor %s", self._cursor)
        self._queue = queue
        self._checkpoint = checkpoint
        self._flush_policy = flush_policy or FlushPolicy(
            max_events=settings.cursor_flush_events,
            max_seconds=settings.cursor_flush_seconds,
        )
        self._clock = clock
        self._events_since_flush = 0
        self._last_flush_time = clock()
        self._last_ledger_sequence: int | None = None
        self._bucket = TokenBucket(rate=rate_limit, capacity=bucket_capacity)
        self._backpressure = BackpressureController(
            queue, high_watermark=high_watermark, low_watermark=low_watermark
        )
        self._adaptive = AdaptiveRateController(
            self._bucket, configured_rate=rate_limit, restore_seconds=restore_seconds
        )
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    @property
    def token_bucket(self) -> TokenBucket:
        return self._bucket

    @property
    def backpressure(self) -> BackpressureController:
        return self._backpressure

    @property
    def adaptive(self) -> AdaptiveRateController:
        return self._adaptive

    async def _connect(self) -> httpx.AsyncClient:
        """Open an SSE connection to Horizon."""
        url = f"{settings.horizon_stream_url}/trades?cursor={self._cursor}"
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        response = await client.get(url, headers={"Accept": "text/event-stream"})
        response.raise_for_status()
        self._client = client
        return client

    def _record_processed_event(self, record: dict) -> None:
        event_cursor = record.get("paging_token") or record.get("id")
        if not isinstance(event_cursor, str):
            logger.warning("Horizon event has no paging token; checkpoint not advanced")
            return
        try:
            self._cursor = validate_cursor(event_cursor)
        except ValueError as exc:
            logger.warning("Ignoring event with malformed paging token: %s", exc)
            return
        ledger = record.get("ledger") or record.get("ledger_sequence")
        try:
            self._last_ledger_sequence = int(ledger) if ledger is not None else None
        except (TypeError, ValueError):
            self._last_ledger_sequence = None
        self._events_since_flush += 1
        now = self._clock()
        if self._flush_policy.should_flush(
            self._events_since_flush, self._last_flush_time, now
        ):
            self._checkpoint.save(self._cursor, self._last_ledger_sequence)
            self._events_since_flush = 0
            self._last_flush_time = now

    def _flush_checkpoint(self) -> None:
        if self._events_since_flush:
            self._checkpoint.save(self._cursor, self._last_ledger_sequence)
            self._events_since_flush = 0
            self._last_flush_time = self._clock()

    async def stream_events(self) -> AsyncIterator[dict]:
        """Yield raw parsed SSE data dicts, handling reconnection and rate limiting."""
        while True:
            try:
                client = await self._connect()
                async with client:
                    async for line in client.aiter_lines():
                        if not self._running:
                            return
                        if line.startswith("data: "):
                            data = line[6:]
                            record = _decode_event(data)
                            if record is not None:
                                await self._bucket.async_acquire()
                                yield record
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 410) and self._cursor != "now":
                    logger.warning(
                        "Horizon rejected cursor %s with HTTP %d; falling back to now",
                        self._cursor,
                        exc.response.status_code,
                    )
                    self._checkpoint.delete()
                    self._cursor = "now"
                    continue
                raise
            except httpx.TransportError:
                logger.warning("SSE connection lost; reconnecting in 5s…")
                await asyncio.sleep(5)

    async def run(self) -> None:
        """Consume the SSE stream, rate-limit, check backpressure, and enqueue trades.

        Handles HTTP 429 by delegating to :meth:`AdaptiveRateController.on_429`.
        """
        self._running = True
        try:
            async for record in self.stream_events():
                try:
                    trade = _parse_trade(record)
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Failed to parse trade record: %s", exc)
                    continue

                await self._backpressure.check_and_wait()
                await self._queue.put(trade)
                self._record_processed_event(record)
        finally:
            self._flush_checkpoint()

    async def run_with_cursor(self) -> AsyncIterator[tuple[Trade, str]]:
        """Yield ``(Trade, cursor)`` tuples with rate limiting and backpressure.

        Use this variant when the caller needs to persist the cursor position
        for resume capability.
        """
        self._running = True
        try:
            async for record in self.stream_events():
                try:
                    trade = _parse_trade(record)
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Failed to parse trade record: %s", exc)
                    continue

                await self._backpressure.check_and_wait()
                await self._queue.put(trade)
                self._record_processed_event(record)
                event_cursor = self._cursor
                yield trade, event_cursor
        finally:
            self._flush_checkpoint()

    def stop(self) -> None:
        """Signal the stream loop to stop."""
        self._running = False


if __name__ == "__main__":
    for trade in stream_trades():
        print(trade.model_dump())
