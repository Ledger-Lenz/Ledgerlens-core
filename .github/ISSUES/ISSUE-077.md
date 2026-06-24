---
title: "Add Token-Bucket Rate Limiting and Backpressure Signalling to Horizon Streamer"
labels: ["difficulty: advanced", "area: ingestion", "type: enhancement"]
assignees: []
---

## Summary

Extend `ingestion/horizon_streamer.py` with a token-bucket rate limiter (configurable: default 50 req/s) and backpressure signalling. When the downstream processing queue exceeds a high-watermark threshold (default: 1,000 items), pause SSE consumption and emit a WARN log. Implement adaptive rate reduction on HTTP 429 responses: halve the configured rate and restore it linearly over 60 seconds. This prevents LedgerLens from overloading the Horizon API during burst periods and protects the downstream scoring pipeline from queue saturation.

## Background & Context

`ingestion/horizon_streamer.py` currently uses a simple per-request delay (`asyncio.sleep`) as a rate limiter. This approach is inadequate for three reasons:

1. **Burstiness**: a fixed delay allows burst consumption during momentary latency spikes (e.g., if the SSE stream delivers 50 events in a single TCP read, the delay is applied once rather than per-event).
2. **No backpressure**: if the downstream feature-engineering and scoring pipeline is slower than the ingest rate, the in-memory queue grows unbounded until OOM.
3. **No HTTP 429 handling**: Horizon responds with `429 Too Many Requests` when rate limits are exceeded. The current implementation either crashes or sleeps for a fixed duration, not adapting the rate.

A token-bucket rate limiter solves (1): tokens refill at a fixed rate (`capacity` per second); each request consumes one token; if no token is available, the caller blocks. This provides burst tolerance up to `bucket_capacity` tokens while maintaining the average rate.

Backpressure (2) is implemented by monitoring `queue.qsize()` against a high-watermark. When exceeded, the SSE consumer coroutine waits until the queue drains below a low-watermark (default: 500 items) before resuming.

Adaptive rate reduction (3): on HTTP 429, halve the `current_rate`; restore to `configured_rate` linearly over `RATE_RESTORE_SECONDS=60` seconds.

## Objectives

- [ ] Implement `TokenBucket` class in `ingestion/rate_limiter.py` with `acquire()` (sync and async variants), `current_rate` property, and `set_rate(new_rate)` method.
- [ ] `TokenBucket.acquire()` blocks until a token is available; non-blocking variant returns `bool`.
- [ ] Integrate `TokenBucket` into `HorizonStreamer`: replace sleep-based rate limiting.
- [ ] Implement `BackpressureController` in `ingestion/horizon_streamer.py` monitoring `queue.qsize()` and pausing/resuming SSE consumption at high/low watermarks.
- [ ] Implement `AdaptiveRateController` that halves `current_rate` on HTTP 429 and restores linearly over `RATE_RESTORE_SECONDS`.
- [ ] Add `HORIZON_RATE_LIMIT`, `HORIZON_QUEUE_HIGH_WATERMARK`, `HORIZON_QUEUE_LOW_WATERMARK`, `RATE_RESTORE_SECONDS` configuration variables.
- [ ] Emit `WARNING` log when backpressure engaged: `"Backpressure: downstream queue at {size} items, pausing SSE consumption"`.
- [ ] Emit `WARNING` log on HTTP 429: `"Horizon HTTP 429: reducing rate to {new_rate:.1f} req/s"`.
- [ ] Emit `INFO` log when rate restored: `"Rate restored to {rate:.1f} req/s after 429 backoff"`.
- [ ] Expose `GET /stream/rate-limiter` endpoint returning `current_rate`, `bucket_level`, `backpressure_active`, `queue_size`.
- [ ] All new code covered by tests; ≥90% branch coverage on `rate_limiter.py`.

## Technical Requirements

### `TokenBucket` class (`ingestion/rate_limiter.py`)

```python
import asyncio
import time
from threading import Lock
from typing import Optional

class TokenBucket:
    """
    Token bucket rate limiter.
    Tokens refill continuously at `rate` tokens/second up to `capacity` tokens.
    """
    def __init__(self, rate: float, capacity: Optional[float] = None):
        """
        rate: tokens per second (refill rate).
        capacity: maximum tokens (default: rate * 2, allowing 2-second bursts).
        """
        self._rate = rate
        self._capacity = capacity or rate * 2.0
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = Lock()

    @property
    def current_rate(self) -> float:
        return self._rate

    def set_rate(self, new_rate: float) -> None:
        with self._lock:
            self._rate = max(new_rate, 0.1)   # floor: 0.1 req/s

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Non-blocking: consume a token if available. Returns True on success."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Blocking: wait until a token is available or timeout expires."""
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            if self.try_acquire():
                return True
            if deadline and time.monotonic() > deadline:
                return False
            time.sleep(min(1.0 / max(self._rate, 0.1), 0.1))

    async def async_acquire(self) -> None:
        """Async blocking version for use in asyncio event loops."""
        while not self.try_acquire():
            await asyncio.sleep(min(1.0 / max(self._rate, 0.1), 0.05))
```

### `BackpressureController` class

```python
class BackpressureController:
    def __init__(
        self,
        queue: "asyncio.Queue",
        high_watermark: int = 1000,
        low_watermark: int = 500,
    ):
        self._queue = queue
        self._high = high_watermark
        self._low = low_watermark
        self._paused = False

    async def check_and_wait(self) -> None:
        """
        Called before each SSE event is enqueued.
        If queue size >= high_watermark, wait until < low_watermark.
        """
        current_size = self._queue.qsize()
        if current_size >= self._high and not self._paused:
            self._paused = True
            logger.warning(
                "Backpressure: downstream queue at %d items, pausing SSE consumption",
                current_size
            )
        if self._paused:
            while self._queue.qsize() > self._low:
                await asyncio.sleep(0.1)
            self._paused = False
            logger.info("Backpressure released: queue drained to %d items", self._queue.qsize())

    @property
    def is_paused(self) -> bool:
        return self._paused
```

### `AdaptiveRateController` class

```python
class AdaptiveRateController:
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

    def on_429(self) -> None:
        """Halve current rate on HTTP 429."""
        new_rate = self._bucket.current_rate / 2.0
        self._bucket.set_rate(new_rate)
        self._last_429_at = time.monotonic()
        logger.warning("Horizon HTTP 429: reducing rate to %.1f req/s", new_rate)

    def tick(self) -> None:
        """
        Call periodically (e.g., every second) to restore rate linearly.
        Restores rate by (configured_rate - current_rate) / restore_seconds per tick.
        """
        if self._last_429_at is None:
            return
        elapsed = time.monotonic() - self._last_429_at
        if elapsed >= self._restore_seconds:
            self._bucket.set_rate(self._configured_rate)
            logger.info("Rate restored to %.1f req/s after 429 backoff", self._configured_rate)
            self._last_429_at = None
        else:
            step = (self._configured_rate - self._bucket.current_rate) * (1.0 / self._restore_seconds)
            self._bucket.set_rate(self._bucket.current_rate + step)
```

### Integration with `HorizonStreamer`

```python
class HorizonStreamer:
    def __init__(self, ..., rate_limit: float = 50.0, ...):
        self._bucket = TokenBucket(rate=rate_limit)
        self._backpressure = BackpressureController(self._queue)
        self._adaptive = AdaptiveRateController(self._bucket, configured_rate=rate_limit)

    async def _stream_loop(self):
        async for event in self._sse_client:
            await self._bucket.async_acquire()
            await self._backpressure.check_and_wait()
            try:
                trade = self._parse_event(event)
                await self._queue.put(trade)
            except HTTP429Error:
                self._adaptive.on_429()
```

### Configuration (`.env.example`)

```
HORIZON_RATE_LIMIT=50             # req/s
HORIZON_RATE_BUCKET_CAPACITY=100  # burst capacity (tokens)
HORIZON_QUEUE_HIGH_WATERMARK=1000
HORIZON_QUEUE_LOW_WATERMARK=500
RATE_RESTORE_SECONDS=60
```

### API endpoint

```python
@router.get("/stream/rate-limiter", response_model=RateLimiterStatus)
async def rate_limiter_status(...):
    ...

class RateLimiterStatus(BaseModel):
    configured_rate: float
    current_rate: float
    bucket_level: float
    backpressure_active: bool
    queue_size: int
    last_429_at: Optional[datetime]
```

## Security Considerations

- Rate limiting is a defence-in-depth measure. An operator misconfiguring `HORIZON_RATE_LIMIT=0` would halt the streamer. Add a validation in `TokenBucket.__init__`: raise `ValueError` if `rate <= 0`.
- The `GET /stream/rate-limiter` endpoint reveals internal queue depth and rate state, which could help an attacker time burst attacks. Gate it behind `LEDGERLENS_ADMIN_API_KEY`.
- `BackpressureController` pauses SSE consumption, potentially causing the Horizon SSE connection to time out. Implement an SSE reconnect on timeout rather than crashing: the streamer should reconnect and resume from the last cursor position.
- Linear rate restoration is bounded by `configured_rate`: `set_rate` must enforce `new_rate <= configured_rate` during restoration to prevent the restoration overshoot exceeding the originally configured limit.

## Testing Requirements

- **Unit — `TokenBucket.try_acquire`**: empty bucket → False; full bucket → True and decrements tokens.
- **Unit — token refill**: advance mock clock by 1 second; assert tokens refilled by `rate`.
- **Unit — `acquire` blocking**: bucket starts empty; mock clock advancing at `rate`; assert `acquire` returns True after ~1 token-refill period.
- **Unit — `BackpressureController` engage**: mock queue with `qsize()=1001`; assert `is_paused=True` after `check_and_wait()`.
- **Unit — `BackpressureController` drain**: paused controller; mock queue draining to 499; assert `is_paused=False` after subsequent `check_and_wait()`.
- **Unit — `AdaptiveRateController.on_429` halves rate**: initial rate=50; call `on_429()`; assert `current_rate ≈ 25`.
- **Unit — linear restoration**: call `on_429()`; advance mock clock by 30s; call `tick()` 30 times; assert rate is approximately midway between 25 and 50.
- **Unit — full restoration**: advance mock clock by 60s; assert rate restored to 50.
- **Unit — rate floor**: call `on_429()` 20 times; assert rate never falls below 0.1.
- **Integration — streamer stops producing when backpressure engaged**: mock queue at high watermark; assert streamer does not enqueue new items.

## Documentation Requirements

- Docstrings on `TokenBucket`, `BackpressureController`, and `AdaptiveRateController`.
- New section in `docs/ingestion.md` (create if not exists): "Rate Limiting and Backpressure" covering algorithm choices and tuning guidance.
- Document `HORIZON_RATE_LIMIT` and related variables in `.env.example`.
- Update README Quick Start / Stream section to note rate limiting.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `TokenBucket` implemented with sync and async `acquire` and linear refill.
- [ ] `BackpressureController` pauses/resumes at configurable watermarks.
- [ ] `AdaptiveRateController` halves rate on 429 and restores linearly.
- [ ] All three components integrated into `HorizonStreamer`.
- [ ] WARNING and INFO logs emitted at correct events.
- [ ] `GET /stream/rate-limiter` endpoint operational and admin-key gated.
- [ ] All unit and integration tests pass; ≥90% branch coverage on `rate_limiter.py`.
- [ ] Rate floor (0.1 req/s) enforced.
- [ ] `docs/ingestion.md` updated.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience implementing or tuning rate limiters in high-throughput ingestion pipelines — specifically token-bucket or leaky-bucket algorithms. You understand the semantics of `asyncio` queues and backpressure in event-driven Python. Familiarity with the Horizon SSE API, HTTP 429 handling, and the LedgerLens ingestion pipeline will accelerate the integration work significantly. Experience with adaptive rate algorithms (multiplicative decrease / additive increase, similar to TCP congestion control) is a strong plus.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., streaming systems, rate limiting, async Python, ingestion pipelines).
2. **Relevant experience**: token-bucket or rate limiter implementations, Horizon API integrations, or backpressure systems you have built.
3. **Approach / thoughts**: would you implement the async variant of `TokenBucket` using an `asyncio.Semaphore` or a custom loop? What is the tradeoff for this use case?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
