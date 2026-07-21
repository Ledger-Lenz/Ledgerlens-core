"""Distributed per-API-key rate limiting shared by the REST and gRPC paths.

Historically, three independent in-process ``dict``s implemented "per-key
rate limiting" in this codebase (``api/gateway.py``, ``detection/api_key_store.py``,
and the deprecated ``api/api_keys_router.py``), none of which shared state
with any other. Under the project's documented horizontally-scaled deployment
(``helm/ledgerlens/values.yaml``: 2-10 replicas) and split REST/gRPC processes,
this meant a key's *effective* ceiling was ``configured_limit x N`` for N
independent enforcing processes rather than ``configured_limit``.

This module provides the single, shared enforcement primitive. It is a
Redis-backed **sliding-window counter** (see :func:`check_rate_limit`),
consulted by every enforcement path via ``detection.api_key_store.check_rate_limit``.

Consistency / accuracy tradeoff
--------------------------------
Sliding-window-counter (chosen) vs. alternatives:

- **Sliding window log** (store every request timestamp, e.g. in a Redis
  ZSET) is exactly accurate but costs O(limit) memory per key and an
  O(log N) Redis operation per request touching potentially thousands of
  members for a high-limit key. Not worth the cost for an abuse-prevention
  control (this is not a billing-grade metering system -- daily/monthly
  quotas already get exact accounting via SQLite ``COUNT`` queries).
- **Token bucket** gives equivalent asymptotic guarantees but requires
  tracking fractional token state and a last-refill timestamp per key,
  and is not noticeably simpler or cheaper in Lua than the counter
  approach below.
- **Sliding window counter** (chosen): O(1) memory (two integers + TTL)
  and exactly one Redis round trip per check, executed atomically via a
  single Lua script (avoiding the classic INCR/EXPIRE race). Worst-case
  overshoot is mathematically bounded at 2x the configured limit (a full
  burst at the very end of one window immediately followed by a full
  burst at the very start of the next); in practice, under any
  traffic that isn't adversarially timed to the exact window boundary,
  observed overshoot is much tighter. This is the standard approach
  described in Cloudflare's and Stripe's public rate-limiting writeups
  and is an appropriate trade for a per-minute abuse control.

Failure mode (explicit, deliberate choice: fail OPEN with a bounded,
observable degradation)
------------------------------------------------------------------------
Rate limiting is a cost/abuse control, not an authn/authz boundary -- unlike
``GatewayMiddleware``'s auth/quota-backend-unreachable path (which fails
*closed*, 503, because an unreachable backend there means the caller's
identity/scope cannot be verified at all), an unreachable Redis here does
not compromise anything: it only means multi-replica accuracy degrades back
to per-process enforcement (today's behavior) for the duration of the
outage. Taking the whole API down because the rate limiter's shared store
is briefly unavailable would be a self-inflicted denial of service far
worse than a temporarily-generous rate limit -- consistent with this
codebase's WAF (``docs/waf_and_rate_limiting.md``) and FeatureStore
(``detection/feature_store.py``) precedent, both of which fail open.

Unlike a silent regression, though: falling back is guarded by a
:class:`~utils.circuit_breaker.CircuitBreaker` (avoids hammering a down
Redis on every request), logs a ``WARNING`` on each open/close transition,
and increments the ``ledgerlens_rate_limiter_fallback_total`` Prometheus
counter on every request served from the fallback path -- so the
degradation is loud, not silent.
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Optional

from config.settings import settings
from utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger("ledgerlens.rate_limiter")

# Consecutive Redis failures before the circuit opens (fallback engaged).
# Lower than FeatureStore's threshold (3) is not warranted here -- same
# tolerance for transient blips, but see the shorter recovery timeout below.
RATE_LIMITER_FAILURE_THRESHOLD = 3

# How long to stay in the fallback (local-only) state before probing Redis
# again. Shorter than FeatureStore's 30s: this is a hot path invoked on
# every scored request (vs. FeatureStore's less frequent hot-tier reads),
# so recovering distributed accuracy sooner matters more, and a failed
# probe is cheap (one extra round trip, not a data-consistency risk).
RATE_LIMITER_RECOVERY_TIMEOUT_SECONDS = 15.0

_DEFAULT_WINDOW_SECONDS = 60.0

# Atomic sliding-window-counter check. Single EVAL call: read-then-write is
# never split across round trips, so no separate process can observe or
# create a partial state (avoids the INCR/EXPIRE TOCTOU race a two-command
# implementation would have).
#
# KEYS[1] = current bucket key (hash-tagged so both keys land on the same
#           Redis Cluster slot -- see _bucket_keys)
# KEYS[2] = previous bucket key
# ARGV[1] = limit (integer, > 0)
# ARGV[2] = window_seconds (integer)
# ARGV[3] = now (float, unix epoch seconds -- wall clock, NOT monotonic:
#           bucket ids must agree across independent processes/hosts, which
#           requires a clock all replicas can compute the same value from.
#           This assumes replicas' clocks are reasonably NTP-synchronized,
#           standard in Kubernetes; clock skew distorts the sliding-window
#           weighting but cannot cause unbounded overshoot since each
#           bucket's own count is still tracked by an atomic per-key INCR)
_SLIDING_WINDOW_LUA = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local elapsed = now % window
local weight = (window - elapsed) / window

local prev_raw = redis.call('GET', KEYS[2])
local prev = 0
if prev_raw then prev = tonumber(prev_raw) end

local curr_raw = redis.call('GET', KEYS[1])
local curr = 0
if curr_raw then curr = tonumber(curr_raw) end

local estimated = (prev * weight) + curr

if estimated >= limit then
    local retry_after = math.floor(window - elapsed) + 1
    return {0, retry_after}
end

local newval = redis.call('INCR', KEYS[1])
if newval == 1 then
    redis.call('EXPIRE', KEYS[1], window * 2)
end

return {1, 0}
"""


def _bucket_keys(key_id: str, window_seconds: float, now: float) -> tuple[str, str]:
    """Return (current_bucket_key, previous_bucket_key) for a sliding window check.

    Keys are hash-tagged (``{key_id}``) so both keys for one check always hash
    to the same Redis Cluster slot -- required for a multi-key Lua script to
    work if this is ever deployed against a clustered Redis.
    """
    bucket = int(now // window_seconds)
    return (
        f"ll:ratelimit:{{{key_id}}}:{bucket}",
        f"ll:ratelimit:{{{key_id}}}:{bucket - 1}",
    )


class DistributedRateLimiter:
    """Redis-backed sliding-window-counter rate limiter with local fallback.

    Mirrors :class:`detection.feature_store.FeatureStore`'s connect-or-fallback
    pattern: on construction it attempts to reach Redis once; if that fails
    (or ``settings.gateway_quota_store == "sqlite"``, the explicit opt-out for
    single-process/dev/test use that avoids the Redis dependency entirely),
    every check is served from an in-process sliding-window dict instead --
    identical to this codebase's pre-fix behavior, just now an explicit,
    observable degraded mode rather than the only mode that ever existed.
    """

    def __init__(self, redis_url: Optional[str] = None, quota_store: Optional[str] = None):
        self.redis_url = redis_url or getattr(settings, "redis_url", None)
        self.quota_store = (quota_store or getattr(settings, "gateway_quota_store", "redis")).lower()

        self.redis_client = None
        self._script = None
        self._using_redis = False

        self._local_windows: dict[str, list[float]] = {}
        self._local_lock = Lock()

        self._circuit = CircuitBreaker(
            name="rate_limiter_redis",
            failure_threshold=RATE_LIMITER_FAILURE_THRESHOLD,
            recovery_timeout=RATE_LIMITER_RECOVERY_TIMEOUT_SECONDS,
            on_open=self._on_circuit_open,
            on_close=self._on_circuit_close,
        )

        if self.quota_store == "redis" and self.redis_url:
            try:
                import redis

                self.redis_client = redis.from_url(
                    self.redis_url,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                self.redis_client.ping()
                self._script = self.redis_client.register_script(_SLIDING_WINDOW_LUA)
                self._using_redis = True
                logger.info("DistributedRateLimiter: connected to Redis at %s", self.redis_url)
            except Exception as e:
                logger.warning(
                    "DistributedRateLimiter: Redis connection failed (%s), starting in "
                    "local-fallback mode -- rate limits will NOT be shared across replicas "
                    "or protocols until Redis becomes reachable",
                    e,
                )
                self.redis_client = None

    # -- observability ----------------------------------------------------

    @property
    def circuit_state(self) -> str:
        """Current Redis circuit breaker state (`closed`/`open`/`half_open`)."""
        return self._circuit.state.value

    @property
    def is_using_redis(self) -> bool:
        """Whether this instance is currently configured to use Redis at all
        (does not reflect transient circuit-open fallback -- use
        :attr:`circuit_state` for live degraded-mode status)."""
        return self._using_redis

    def _on_circuit_open(self) -> None:
        logger.warning(
            "rate_limiter_fallback_engaged reason=redis_unavailable "
            "effect=per_process_only_enforcement scope=all_keys"
        )

    def _on_circuit_close(self) -> None:
        logger.info("rate_limiter_fallback_cleared reason=redis_recovered")

    def _redis_available(self) -> bool:
        return bool(self._using_redis and self.redis_client) and self._circuit.allow_request()

    # -- enforcement --------------------------------------------------------

    def check(
        self, key_id: str, limit_per_minute: int, window_seconds: float = _DEFAULT_WINDOW_SECONDS
    ) -> tuple[bool, int]:
        """Check and record one request against *key_id*'s sliding window.

        Returns ``(allowed, retry_after_seconds)``. ``limit_per_minute <= 0``
        means unlimited (matches the ``daily_quota``/``monthly_quota`` "0 =
        unlimited" convention used elsewhere in this store).
        """
        if limit_per_minute <= 0:
            return True, 0

        if self._redis_available():
            try:
                now = time.time()
                cur_key, prev_key = _bucket_keys(key_id, window_seconds, now)
                result = self._script(
                    keys=[cur_key, prev_key],
                    args=[limit_per_minute, int(window_seconds), now],
                )
                self._circuit.record_success()
                _emit_check_metric(backend="redis")
                return bool(int(result[0])), int(result[1])
            except Exception as e:
                self._circuit.record_failure()
                logger.debug("DistributedRateLimiter: Redis check failed (%s), falling back", e)

        _emit_fallback_metric()
        _emit_check_metric(backend="local")
        return self._check_local(key_id, limit_per_minute, window_seconds)

    def _check_local(
        self, key_id: str, limit_per_minute: int, window_seconds: float
    ) -> tuple[bool, int]:
        """Per-process sliding-window-log fallback (exact within one process).

        Identical algorithm to this codebase's pre-fix implementation --
        used only while Redis is unreachable, so degraded-mode behavior is
        never worse than what previously shipped as the *only* mode.
        """
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._local_lock:
            timestamps = [t for t in self._local_windows.get(key_id, []) if t > cutoff]
            if len(timestamps) >= limit_per_minute:
                oldest = timestamps[0]
                retry_after = int(window_seconds - (now - oldest)) + 1
                self._local_windows[key_id] = timestamps
                return False, retry_after
            timestamps.append(now)
            self._local_windows[key_id] = timestamps
            return True, 0


def _emit_fallback_metric() -> None:
    try:
        from api.metrics import ledgerlens_rate_limiter_fallback_total

        ledgerlens_rate_limiter_fallback_total.inc()
    except Exception:
        pass


def _emit_check_metric(backend: str) -> None:
    try:
        from api.metrics import ledgerlens_rate_limiter_checks_total

        ledgerlens_rate_limiter_checks_total.labels(backend=backend).inc()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process-wide singleton (one Redis connection pool per process, same
# pattern as detection.feature_store's module-level FeatureStore usage)
# ---------------------------------------------------------------------------

_limiter: Optional[DistributedRateLimiter] = None
_limiter_lock = Lock()


def get_rate_limiter() -> DistributedRateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = DistributedRateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    """Drop the singleton so the next call re-reads settings and reconnects.

    Used by tests that patch ``settings.redis_url`` / ``settings.gateway_quota_store``.
    """
    global _limiter
    with _limiter_lock:
        _limiter = None


def check_rate_limit(
    key_id: str, limit_per_minute: int, window_seconds: float = _DEFAULT_WINDOW_SECONDS
) -> tuple[bool, int]:
    """Shared entry point used by every enforcement path (REST gateway,
    the legacy ``require_scope`` dependency, and gRPC's ``_authenticate``).

    Returns ``(allowed, retry_after_seconds)``.
    """
    return get_rate_limiter().check(key_id, limit_per_minute, window_seconds)
