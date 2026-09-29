"""Token-bucket rate limiter, backpressure controller, and adaptive rate reduction.

Provides three cooperating components:

- :class:`TokenBucket` — a lock-based token bucket that enforces an average
  rate (tokens/second) while permitting bursts up to *capacity*.
- :class:`BackpressureController` — monitors an :class:`asyncio.Queue` and
  pauses SSE consumption when the queue exceeds a high-watermark threshold.
- :class:`AdaptiveRateController` — halves the token-bucket rate on HTTP 429
  responses and restores it linearly over a configurable window.
- :class:`HorizonAdaptiveRateLimiter` — async token bucket whose refill rate
  is continuously re-derived from Horizon's ``X-Ratelimit-*`` response headers,
  with 429-driven backoff-and-resume as a safety net.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Optional

logger = logging.getLogger("ledgerlens.rate_limiter")

__all__ = [
    "TokenBucket",
    "BackpressureController",
    "AdaptiveRateController",
    "HorizonQuota",
    "HorizonAdaptiveRateLimiter",
    "parse_horizon_rate_limit_headers",
]

# Minimum allowed refill rate — prevents the bucket from being silenced to zero.
_MIN_RATE = 0.1  # tokens/second


class TokenBucket:
    """Token-bucket rate limiter.

    Tokens refill continuously at ``rate`` tokens/second up to ``capacity``
    tokens.  Call :meth:`acquire` or :meth:`async_acquire` before each
    request to block until a token is available.

    Parameters
    ----------
    rate:
        Tokens per second (refill rate). Must be > 0.
    capacity:
        Maximum token count (default: ``rate * 2``, allowing 2-second bursts).

    Raises
    ------
    ValueError
        If ``rate <= 0``.
    """

    def __init__(self, rate: float, capacity: Optional[float] = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._rate = rate
        self._capacity = capacity or rate * 2.0
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = Lock()

    @property
    def current_rate(self) -> float:
        return self._rate

    @property
    def bucket_level(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def capacity(self) -> float:
        return self._capacity

    def set_rate(self, new_rate: float) -> None:
        """Update the refill rate.

        The new rate is clamped to a minimum of :data:`_MIN_RATE` (0.1 req/s)
        so the bucket can never be silenced entirely.
        """
        with self._lock:
            self._rate = max(new_rate, _MIN_RATE)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Non-blocking: consume a token if available. Returns ``True`` on success."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Blocking: wait until a token is available or *timeout* expires.

        Returns ``True`` if a token was acquired, ``False`` on timeout.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait. ``None`` means wait indefinitely.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            if self.try_acquire():
                return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(min(1.0 / max(self._rate, _MIN_RATE), 0.1))

    async def async_acquire(self) -> None:
        """Async blocking version for use in asyncio event loops."""
        while not self.try_acquire():
            await asyncio.sleep(min(1.0 / max(self._rate, _MIN_RATE), 0.05))


class BackpressureController:
    """Monitors an :class:`asyncio.Queue` and pauses consumption when it grows too large.

    Parameters
    ----------
    queue:
        The downstream processing queue to monitor.
    high_watermark:
        Queue size at which backpressure engages (default 1000).
    low_watermark:
        Queue size at which consumption resumes (default 500).
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        high_watermark: int = 1000,
        low_watermark: int = 500,
    ) -> None:
        self._queue = queue
        self._high = high_watermark
        self._low = low_watermark
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def check_and_wait(self) -> None:
        """Called before each SSE event is enqueued.

        If queue size >= *high_watermark* and backpressure is not already
        active, set the paused flag and wait until the queue drains below
        *low_watermark*.

        The ``_paused`` flag is set *before* entering the drain loop so that
        concurrent callers arriving while draining bypass the watermark check
        and fall directly into the shared wait, preventing a double-log storm.
        """
        if not self._paused and self._queue.qsize() >= self._high:
            self._paused = True
            logger.warning(
                "Backpressure: downstream queue at %d items, pausing SSE consumption",
                self._queue.qsize(),
            )
        if self._paused:
            while self._queue.qsize() > self._low:
                await asyncio.sleep(0.1)
            self._paused = False
            logger.info(
                "Backpressure released: queue drained to %d items",
                self._queue.qsize(),
            )


class AdaptiveRateController:
    """Reduces the token-bucket rate on HTTP 429 and restores it over time.

    Parameters
    ----------
    bucket:
        The :class:`TokenBucket` whose rate to adjust.
    configured_rate:
        The original (configured) rate; the controller restores toward this.
    restore_seconds:
        Duration (seconds) over which to linearly restore the rate after a 429.
    """

    def __init__(
        self,
        bucket: TokenBucket,
        configured_rate: float,
        restore_seconds: float = 60.0,
    ) -> None:
        self._bucket = bucket
        self._configured_rate = configured_rate
        self._restore_seconds = restore_seconds
        self._last_429_at: Optional[float] = None

    @property
    def last_429_at(self) -> Optional[float]:
        return self._last_429_at

    def on_429(self) -> None:
        """Halve the current rate on HTTP 429 response."""
        new_rate = self._bucket.current_rate / 2.0
        self._bucket.set_rate(new_rate)
        self._last_429_at = time.monotonic()
        logger.warning(
            "Horizon HTTP 429: reducing rate to %.1f req/s (clamped to minimum %.1f)",
            new_rate,
            _MIN_RATE,
        )

    def tick(self) -> None:
        """Call periodically (e.g., every second) to restore the rate linearly.

        Restores toward *configured_rate* at a pace of
        ``(configured_rate - current_rate) / restore_seconds`` per tick.
        No-op when no 429 has been received.
        """
        if self._last_429_at is None:
            return
        elapsed = time.monotonic() - self._last_429_at
        if elapsed >= self._restore_seconds:
            self._bucket.set_rate(self._configured_rate)
            logger.info(
                "Rate restored to %.1f req/s after 429 backoff",
                self._configured_rate,
            )
            self._last_429_at = None
        else:
            step = (self._configured_rate - self._bucket.current_rate) * (
                1.0 / self._restore_seconds
            )
            self._bucket.set_rate(self._bucket.current_rate + step)


@dataclass(frozen=True)
class HorizonQuota:
    """Rate-limit state reported by Horizon on a single response."""

    limit: int            # requests allowed per window (X-Ratelimit-Limit)
    remaining: int        # requests left in the window (X-Ratelimit-Remaining)
    reset_seconds: float  # seconds until the window resets (X-Ratelimit-Reset)

    @property
    def utilization(self) -> float:
        """Fraction of the window quota already consumed (0.0-1.0)."""
        return (self.limit - self.remaining) / self.limit if self.limit > 0 else 0.0


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, val in headers.items():
            if key.lower() == lowered:
                return val
    return value


def parse_horizon_rate_limit_headers(headers: Mapping[str, str]) -> HorizonQuota | None:
    """Parse Horizon's ``X-Ratelimit-Limit/Remaining/Reset`` headers.

    Returns ``None`` when any header is missing or malformed.
    """
    try:
        limit = int(_header(headers, "X-Ratelimit-Limit"))  # type: ignore[arg-type]
        remaining = int(_header(headers, "X-Ratelimit-Remaining"))  # type: ignore[arg-type]
        reset = float(_header(headers, "X-Ratelimit-Reset"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if limit <= 0 or remaining < 0 or reset < 0:
        return None
    return HorizonQuota(limit=limit, remaining=min(remaining, limit), reset_seconds=reset)


class HorizonAdaptiveRateLimiter:
    """Async token bucket tuned by Horizon's reported rate-limit headers.

    After every response, :meth:`observe_response` re-derives the refill rate
    as ``safety_factor * remaining / reset_seconds`` so the remaining quota is
    spread evenly across the rest of the window — spending spare quota when
    Horizon allows more and slowing down when it is nearly exhausted, without
    manual tuning.  When ``remaining`` hits zero, requests pause until the
    window resets.

    As a safety net, :meth:`on_429` halves the rate and pauses all callers for
    ``Retry-After`` seconds (or an exponential backoff when absent); the pause
    lifts automatically and header-driven tuning resumes.

    Parameters
    ----------
    rate:
        Initial refill rate (req/s) used until the first headers are seen.
    burst:
        Bucket capacity (default ``rate * 2``).
    max_rate:
        Optional hard ceiling on the header-derived rate.
    safety_factor:
        Fraction of the header-derived sustainable rate actually used.
    clock, sleep:
        Injectable time source and sleep coroutine (for tests / simulation).
    """

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        max_rate: float | None = None,
        safety_factor: float = 0.9,
        max_backoff: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate!r}")
        self._rate = rate
        self._capacity = burst if burst is not None else rate * 2
        self._max_rate = max_rate
        self._safety = safety_factor
        self._max_backoff = max_backoff
        self._clock = clock
        self._sleep = sleep
        self._tokens = self._capacity
        self._last_refill = clock()
        self._paused_until = 0.0
        self._consecutive_429 = 0
        self._lock = asyncio.Lock()
        self._sent: deque[float] = deque()
        self.quota: HorizonQuota | None = None

    @property
    def current_rate(self) -> float:
        return self._rate

    @property
    def paused_until(self) -> float:
        return self._paused_until

    def effective_throughput(self, window: float = 60.0) -> float:
        """Requests per second actually dispatched over the trailing *window*."""
        cutoff = self._clock() - window
        while self._sent and self._sent[0] < cutoff:
            self._sent.popleft()
        return len(self._sent) / window

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last_refill) * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        async with self._lock:
            pause = self._paused_until - self._clock()
            if pause > 0:
                await self._sleep(pause)
                self._last_refill = max(self._last_refill, self._clock())
            self._refill()
            if self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                self._refill()
            self._tokens = max(0.0, self._tokens - 1.0)
            self._sent.append(self._clock())
        self._publish_metrics()

    def observe_response(self, status_code: int, headers: Mapping[str, str]) -> None:
        """Feed a Horizon response into the limiter (call for every response)."""
        if status_code == 429:
            self.on_429(_retry_after(headers))
            return
        self._consecutive_429 = 0
        quota = parse_horizon_rate_limit_headers(headers)
        if quota is None:
            return
        self.quota = quota
        now = self._clock()
        if quota.remaining == 0:
            self._paused_until = max(self._paused_until, now + quota.reset_seconds)
            logger.warning("Horizon quota exhausted; pausing %.1fs until window reset", quota.reset_seconds)
        target = self._safety * quota.remaining / max(quota.reset_seconds, 1.0)
        if self._max_rate is not None:
            target = min(target, self._max_rate)
        self._refill()
        self._rate = max(target, _MIN_RATE)
        # Never hold more burst tokens than the window has left.
        self._tokens = min(self._tokens, float(quota.remaining))
        self._publish_metrics()

    def on_429(self, retry_after: float | None = None) -> None:
        """Back off on HTTP 429: halve the rate and pause all callers."""
        self._consecutive_429 += 1
        delay = retry_after if retry_after is not None else min(
            self._max_backoff, 2.0 ** (self._consecutive_429 - 1)
        )
        delay = min(delay, self._max_backoff)
        self._refill()
        self._rate = max(self._rate / 2.0, _MIN_RATE)
        self._tokens = 0.0
        self._paused_until = max(self._paused_until, self._clock() + delay)
        logger.warning(
            "Horizon HTTP 429: pausing %.1fs and reducing rate to %.2f req/s", delay, self._rate
        )
        from ingestion.metrics import get_metrics

        get_metrics().http_rate_limit_backoffs_total.inc()
        self._publish_metrics()

    def _publish_metrics(self) -> None:
        from ingestion.metrics import get_metrics

        metrics = get_metrics()
        metrics.http_rate_limit_allowed_rps.set(self._rate)
        metrics.http_effective_throughput_rps.set(self.effective_throughput())
        if self.quota is not None:
            metrics.http_quota_utilization_ratio.set(self.quota.utilization)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = _header(headers, "Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None
