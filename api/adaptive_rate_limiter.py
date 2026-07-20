import logging
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from config.settings import settings

logger = logging.getLogger("ledgerlens.adaptive_rate")


class AdaptiveNamespaceRateLimiter:
    def __init__(
        self, tighten_factor: Optional[float] = None, abuse_window_seconds: Optional[int] = None, abuse_threshold: Optional[int] = None):
        self.tighten_factor = tighten_factor or settings.adaptive_rate_tighten_factor
        self.abuse_window_seconds = abuse_window_seconds or settings.adaptive_rate_abuse_window_seconds
        self.abuse_threshold = abuse_threshold or settings.adaptive_rate_abuse_threshold
        self._lock = Lock()
        # key: { key_id: deque(timestamps)}
        self._abuse_signals: dict[str, deque[float]] = defaultdict(deque)
        # key: { key_id: (tightened_at)}
        self._tightened_keys: dict[str, float] = {}

    def record_response(self, key_id: str, status_code: int, waf_blocked: bool, namespace_id: str = "") -> None:
        """Record a response to track abuse signals (4xx responses or WAF blocks."""
        now = time.monotonic()
        with self._lock:
            cleanup_threshold = now - self.abuse_window_seconds
            # Clean up old signals
            signals = self._abuse_signals[key_id]
            while signals and signals[0] < cleanup_threshold:
                signals.popleft()
            # Add new signal if 4xx or WAF blocked
            if waf_blocked or (400 <= status_code < 500):
                signals.append(now)
            # Check if we need to tighten
            if len(signals) >= self.abuse_threshold and key_id not in self._tightened_keys:
                self._tightened_keys[key_id] = now
                try:
                    from api.metrics import ledgerlens_adaptive_rate_limit_tightened_total
                    ledgerlens_adaptive_rate_limit_tightened_total.labels(namespace_id=namespace_id).inc()
                except Exception as e:
                    logger.error("Failed to increment adaptive rate metric: %s", e)
                logger.warning("Adaptive rate limit tightened for key_id=%s, namespace=%s", key_id, namespace_id)

    def effective_limit(self, key_id: str, configured_limit: int) -> int:
        """Return the effective rate limit for a key, considering any tightening."""
        now = time.monotonic()
        with self._lock:
            tightened_at = self._tightened_keys.get(key_id)
            if tightened_at is None:
                return configured_limit
            # Check if tightening is still active (within window)
            cleanup_threshold = now - self.abuse_window_seconds
            if tightened_at < cleanup_threshold:
                del self._tightened_keys[key_id]
                return configured_limit
            # Return tightened limit (never less than 1)
            return max(1, int(configured_limit * self.tighten_factor))


# Singleton instance
_adaptive_limiter = AdaptiveNamespaceRateLimiter()


def get_adaptive_limiter() -> AdaptiveNamespaceRateLimiter:
    return _adaptive_limiter
