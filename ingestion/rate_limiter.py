"""Token-bucket rate limiter with async support for Horizon SSE ingestion."""

import asyncio
import time
from threading import Lock
from typing import Optional


class TokenBucket:
    """Token bucket rate limiter.

    Tokens refill continuously at `rate` tokens/second up to `capacity` tokens.
    """

    def __init__(self, rate: float, capacity: Optional[float] = None):
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
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

    def set_rate(self, new_rate: float) -> None:
        with self._lock:
            self._rate = max(new_rate, 0.1)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Non-blocking: consume a token if available."""
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
