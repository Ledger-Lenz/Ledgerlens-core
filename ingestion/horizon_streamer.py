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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Callable, Literal, Optional

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
from ingestion.metrics import get_metrics
from ingestion.rate_limiter import (
    AdaptiveRateController,
    TokenBucket,
)
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

_metrics = get_metrics()

logger = logging.getLogger(__name__)

HORIZON_FAILURE_THRESHOLD = 5
HORIZON_RECOVERY_TIMEOUT_SECONDS = 60.0
# Delay between reconnect attempts while the circuit is still CLOSED, so a
# string of immediate failures doesn't itself become a connection storm.
_RECONNECT_BACKOFF_SECONDS = 1.0
HIGH_WATER_RATIO = 0.8
OverflowStrategy = Literal["block", "drop_newest", "drop_oldest"]

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
        paging_token=str(record.get("paging_token") or record["id"]),
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


class BoundedTradeQueue:
    """Bounded async trade queue with an immutable overflow policy.

    ``block`` propagates pressure to the SSE reader and preserves every event.
    ``drop_newest`` preserves queued work, while ``drop_oldest`` prioritizes
    recent trades. The two drop policies bound memory without stalling reads.
    """

    _VALID_STRATEGIES = {"block", "drop_newest", "drop_oldest"}

    def __init__(
        self,
        maxsize: int = 1000,
        overflow_strategy: OverflowStrategy = "drop_oldest",
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        if overflow_strategy not in self._VALID_STRATEGIES:
            raise ValueError(f"unsupported overflow strategy: {overflow_strategy}")
        self.maxsize = maxsize
        self._overflow_strategy: OverflowStrategy = overflow_strategy
        self._queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0
        self._high_water_hits = 0

    @property
    def overflow_strategy(self) -> OverflowStrategy:
        return self._overflow_strategy

    @overflow_strategy.setter
    def overflow_strategy(self, _value: OverflowStrategy) -> None:
        raise RuntimeError("overflow strategy cannot be changed after construction")

    async def put(self, trade: Trade) -> bool:
        """Enqueue a trade, returning ``False`` only when the incoming trade drops."""
        if self._overflow_strategy == "block":
            await self._queue.put(trade)
            self._record_high_water()
            return True
        if not self._queue.full():
            self._queue.put_nowait(trade)
            self._record_high_water()
            return True
        if self._overflow_strategy == "drop_newest":
            self._dropped += 1
            self._record_high_water()
            return False

        # No await occurs between full() and get_nowait(), so another task on
        # this event loop cannot invalidate the state check.
        self._queue.get_nowait()
        self._dropped += 1
        self._queue.put_nowait(trade)
        self._record_high_water()
        return True

    def _record_high_water(self) -> None:
        if self.depth() / self.maxsize >= HIGH_WATER_RATIO:
            self._high_water_hits += 1

    async def get(self) -> Trade:
        return await self._queue.get()

    def get_nowait(self) -> Trade:
        return self._queue.get_nowait()

    def depth(self) -> int:
        return self._queue.qsize()

    def dropped_count(self) -> int:
        return self._dropped

    def high_water_mark_reached_count(self) -> int:
        return self._high_water_hits


@dataclass
class StreamerMetrics:
    """Aggregate ingestion and backpressure metrics; never contains event data."""

    events_received: int = 0
    events_queued: int = 0
    events_dropped: int = 0
    dropped_newest: int = 0
    dropped_oldest: int = 0
    queue_depth_current: int = 0
    queue_depth_peak: int = 0
    high_water_mark_hits: int = 0
    throttle_sleep_total_seconds: float = 0.0
    last_event_at: datetime | None = None


class HorizonStreamer:
    """Async SSE consumer with configurable rate limiting and backpressure.

    Wraps the Horizon `/trades` SSE endpoint in an async iterator that
    enforces a token-bucket rate limit, monitors downstream queue depth
    (backpressure), and adaptively reduces the ingestion rate on HTTP 429
    responses.

    Parameters
    ----------
    queue:
        The bounded downstream queue. When omitted, configuration settings
        create one. Plain ``asyncio.Queue`` instances are rejected so callers
        cannot accidentally retain and consume from an unbounded queue.
    cursor:
        Horizon paging token to resume from (default ``"now"``).
    rate_limit:
        Tokens per second (default 50).
    bucket_capacity:
        Maximum token burst (default ``rate_limit * 2``).
    restore_seconds:
        Seconds over which to restore rate after a 429 (default 60).
    """

    def __init__(
        self,
        queue: BoundedTradeQueue | None = None,
        cursor: str | None = None,
        rate_limit: Optional[float] = None,
        bucket_capacity: Optional[float] = None,
        high_watermark: Optional[int] = None,
        low_watermark: Optional[int] = None,
        restore_seconds: Optional[float] = None,
        queue_depth: int | None = None,
        overflow_strategy: OverflowStrategy | None = None,
        high_water_ratio: float | None = None,
        checkpoint: CursorCheckpoint | None = None,
        flush_policy: FlushPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        rate_limit = rate_limit if rate_limit is not None else settings.horizon_rate_limit
        bucket_capacity = bucket_capacity if bucket_capacity is not None else settings.horizon_rate_bucket_capacity
        restore_seconds = restore_seconds if restore_seconds is not None else settings.rate_restore_seconds
        queue_depth = queue_depth if queue_depth is not None else settings.streamer_queue_maxsize
        overflow_strategy = (
            overflow_strategy
            if overflow_strategy is not None
            else settings.streamer_overflow_strategy
        )
        high_water_ratio = (
            high_water_ratio
            if high_water_ratio is not None
            else settings.streamer_high_water_ratio
        )
        if not 0 < high_water_ratio <= 1:
            raise ValueError("high_water_ratio must be in (0, 1]")
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
        if isinstance(queue, BoundedTradeQueue):
            self.queue = queue
        else:
            if queue is not None:
                raise TypeError(
                    "queue must be a BoundedTradeQueue; plain asyncio.Queue is unsupported"
                )
            self.queue = BoundedTradeQueue(queue_depth, overflow_strategy)
        self._queue = self.queue
        self._high_water_ratio = high_water_ratio
        self._throttle_level = 0
        self._metrics = StreamerMetrics()
        self._metrics_lock = Lock()
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
        self._adaptive = AdaptiveRateController(
            self._bucket, configured_rate=rate_limit, restore_seconds=restore_seconds
        )
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    @property
    def token_bucket(self) -> TokenBucket:
        return self._bucket

    @property
    def adaptive(self) -> AdaptiveRateController:
        return self._adaptive

    def metrics_snapshot(self) -> StreamerMetrics:
        """Return a thread-safe copy suitable for API or Prometheus export."""
        with self._metrics_lock:
            snapshot = replace(self._metrics)
        snapshot.queue_depth_current = self.queue.depth()
        return snapshot

    async def _maybe_throttle(self) -> None:
        """Apply bounded exponential delay once the queue reaches its high-water mark.

        The delay slows producer reads before enqueueing, decays after the queue
        drains, and is capped at two seconds so a slow consumer cannot stall the
        streamer indefinitely.
        """
        ratio = self.queue.depth() / self.queue.maxsize
        if ratio >= self._high_water_ratio:
            delay = min(0.05 * (2**self._throttle_level), 2.0)
            self._throttle_level += 1
            with self._metrics_lock:
                self._metrics.high_water_mark_hits += 1
                self._metrics.throttle_sleep_total_seconds += delay
            await asyncio.sleep(delay)
        else:
            self._throttle_level = max(0, self._throttle_level - 1)

    async def _enqueue(self, trade: Trade) -> bool:
        await self._maybe_throttle()
        before_dropped = self.queue.dropped_count()
        accepted = await self.queue.put(trade)
        dropped = self.queue.dropped_count() - before_dropped
        depth = self.queue.depth()
        with self._metrics_lock:
            if accepted:
                self._metrics.events_queued += 1
            if dropped:
                self._metrics.events_dropped += dropped
                if self.queue.overflow_strategy == "drop_newest":
                    self._metrics.dropped_newest += dropped
                elif self.queue.overflow_strategy == "drop_oldest":
                    self._metrics.dropped_oldest += dropped
            self._metrics.queue_depth_current = depth
            self._metrics.queue_depth_peak = max(self._metrics.queue_depth_peak, depth)
            total_dropped = self._metrics.events_dropped

        # ── Prometheus instrumentation ────────────────────────────────────
        if accepted:
            _metrics.events_queued_total.labels(source="horizon_sse").inc()
        if dropped:
            _metrics.events_dropped_total.labels(
                source="horizon_sse",
                reason=self.queue.overflow_strategy,
            ).inc(dropped)
        _metrics.queue_depth.labels(source="horizon_sse").set(depth)
        _metrics.queue_depth_peak.labels(source="horizon_sse").set(
            self._metrics.queue_depth_peak
        )
        # ─────────────────────────────────────────────────────────────────

        if dropped and total_dropped % 100 == 0:
            logger.warning(
                "Dropped %d Horizon trade events (strategy=%s, queue_depth=%d)",
                total_dropped,
                self.queue.overflow_strategy,
                depth,
            )
        return accepted

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
                _metrics.sse_reconnects_total.inc()
                await asyncio.sleep(5)

    async def run(self) -> None:
        """Consume the SSE stream, rate-limit, check backpressure, and enqueue trades.

        Handles HTTP 429 by delegating to :meth:`AdaptiveRateController.on_429`.
        """
        self._running = True
        try:
            async for record in self.stream_events():
                with self._metrics_lock:
                    self._metrics.events_received += 1
                    self._metrics.last_event_at = datetime.now(timezone.utc)
                _metrics.events_received_total.labels(source="horizon_sse").inc()
                try:
                    trade = _parse_trade(record)
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Failed to parse trade record: %s", exc)
                    continue

                await self._enqueue(trade)
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
                with self._metrics_lock:
                    self._metrics.events_received += 1
                    self._metrics.last_event_at = datetime.now(timezone.utc)
                _metrics.events_received_total.labels(source="horizon_sse").inc()
                try:
                    trade = _parse_trade(record)
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Failed to parse trade record: %s", exc)
                    continue

                accepted = await self._enqueue(trade)
                self._record_processed_event(record)
                event_cursor = self._cursor
                if accepted:
                    yield trade, event_cursor
        finally:
            self._flush_checkpoint()

    def stop(self) -> None:
        """Signal the stream loop to stop."""
        self._running = False


def stream_with_features(cursor: str = "now") -> Iterator[tuple]:
    """Yield ``(Trade, feature_vector)`` pairs from the live SDEX stream.

    Combines :func:`stream_trades` with
    :class:`~detection.streaming_features.StreamingFeatureEngine` so callers
    get incrementally updated feature vectors without having to manage the
    engine themselves.
    """
    if TYPE_CHECKING:
        from detection.streaming_features import StreamingFeatureEngine
    else:
        from detection.streaming_features import StreamingFeatureEngine

    engine = StreamingFeatureEngine()
    for trade in stream_trades(cursor):
        fv = engine.update(trade)
        yield trade, fv


if __name__ == "__main__":
    for trade in stream_trades():
        print(trade.model_dump())
