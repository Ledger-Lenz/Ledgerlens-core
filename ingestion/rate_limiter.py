"""Token-bucket rate limiter, backpressure controller, and adaptive rate reduction.

Provides three cooperating components:

- :class:`TokenBucket` — a lock-based token bucket that enforces an average
  rate (tokens/second) while permitting bursts up to *capacity*.
- :class:`BackpressureController` — monitors an :class:`asyncio.Queue` and
  pauses SSE consumption when the queue exceeds a high-watermark threshold.
- :class:`AdaptiveRateController` — halves the token-bucket rate on HTTP 429
  responses and restores it linearly over a configurable window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("ledgerlens.rate_limiter")


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
        """Update the refill rate (clamped to a minimum of 0.1 req/s)."""
        with self._lock:
            self._rate = max(new_rate, 0.1)

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
        """
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

        If queue size >= *high_watermark*, wait until it drains below
        *low_watermark* before returning.
        """
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
            "Horizon HTTP 429: reducing rate to %.1f req/s",
            new_rate,
        )

    def tick(self) -> None:
        """Call periodically (e.g., every second) to restore the rate linearly.

        Restores toward *configured_rate* at a pace of
        ``(configured_rate - current_rate) / restore_seconds`` per tick.
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
            remaining = self._restore_seconds - elapsed
            step = (self._configured_rate - self._bucket.current_rate) * (
                1.0 / self._restore_seconds
            )
            self._bucket.set_rate(self._bucket.current_rate + step)
