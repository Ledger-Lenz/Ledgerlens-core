"""Tests for HorizonAdaptiveRateLimiter (ingestion/rate_limiter.py).

Uses a simulated clock and a simulated Horizon server enforcing a fixed
per-window quota, so the tests are deterministic and fast.
"""
import asyncio

from ingestion.rate_limiter import (
    HorizonAdaptiveRateLimiter,
    TokenBucket,
    parse_horizon_rate_limit_headers,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class FakeHorizon:
    """Enforces *limit* requests per *window* seconds, like Horizon's per-IP limit."""

    def __init__(self, clock: FakeClock, limit: int, window: float) -> None:
        self.clock, self.limit, self.window = clock, limit, window
        self.window_start = 0.0
        self.used = 0
        self.ok = 0
        self.throttled = 0

    def request(self) -> tuple[int, dict[str, str]]:
        now = self.clock()
        if now - self.window_start >= self.window:
            self.window_start += self.window * ((now - self.window_start) // self.window)
            self.used = 0
        reset = self.window - (now - self.window_start)
        if self.used >= self.limit:
            self.throttled += 1
            return 429, {"Retry-After": f"{reset:.3f}"}
        self.used += 1
        self.ok += 1
        return 200, {
            "X-Ratelimit-Limit": str(self.limit),
            "X-Ratelimit-Remaining": str(self.limit - self.used),
            "X-Ratelimit-Reset": f"{reset:.3f}",
        }


def _run_adaptive(clock, server, limiter, duration):
    async def loop():
        while clock() < duration:
            await limiter.acquire()
            status, headers = server.request()
            limiter.observe_response(status, headers)

    asyncio.run(loop())


def test_parse_headers_case_insensitive():
    quota = parse_horizon_rate_limit_headers(
        {"x-ratelimit-limit": "3600", "x-ratelimit-remaining": "900", "x-ratelimit-reset": "30"}
    )
    assert quota is not None
    assert quota.limit == 3600 and quota.remaining == 900 and quota.reset_seconds == 30
    assert quota.utilization == 0.75


def test_parse_headers_missing_or_malformed():
    assert parse_horizon_rate_limit_headers({}) is None
    assert parse_horizon_rate_limit_headers(
        {"X-Ratelimit-Limit": "abc", "X-Ratelimit-Remaining": "1", "X-Ratelimit-Reset": "1"}
    ) is None


def test_rate_adapts_to_changing_headers():
    clock = FakeClock()
    limiter = HorizonAdaptiveRateLimiter(rate=5.0, clock=clock, sleep=clock.sleep)
    limiter.observe_response(
        200, {"X-Ratelimit-Limit": "3600", "X-Ratelimit-Remaining": "3000", "X-Ratelimit-Reset": "100"}
    )
    high = limiter.current_rate
    assert high > 5.0  # spends spare quota without manual tuning
    limiter.observe_response(
        200, {"X-Ratelimit-Limit": "3600", "X-Ratelimit-Remaining": "50", "X-Ratelimit-Reset": "100"}
    )
    assert limiter.current_rate < high
    assert limiter.current_rate <= 0.5


def test_quota_exhaustion_pauses_until_reset():
    clock = FakeClock()
    limiter = HorizonAdaptiveRateLimiter(rate=5.0, clock=clock, sleep=clock.sleep)
    limiter.observe_response(
        200, {"X-Ratelimit-Limit": "10", "X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "12"}
    )
    asyncio.run(limiter.acquire())
    assert clock() >= 12.0


def test_429_backs_off_and_resumes():
    clock = FakeClock()
    limiter = HorizonAdaptiveRateLimiter(rate=8.0, clock=clock, sleep=clock.sleep)
    limiter.observe_response(429, {"Retry-After": "5"})
    assert limiter.current_rate == 4.0
    asyncio.run(limiter.acquire())
    assert clock() >= 5.0
    # Header-driven tuning resumes after the pause.
    limiter.observe_response(
        200, {"X-Ratelimit-Limit": "3600", "X-Ratelimit-Remaining": "3500", "X-Ratelimit-Reset": "100"}
    )
    assert limiter.current_rate > 8.0


def test_sustained_load_variable_limits_never_exceeds_quota():
    clock = FakeClock()
    server = FakeHorizon(clock, limit=600, window=60.0)
    limiter = HorizonAdaptiveRateLimiter(rate=1.0, clock=clock, sleep=clock.sleep)
    _run_adaptive(clock, server, limiter, duration=300.0)
    # Horizon then tightens the limit mid-run; limiter must follow without 429 storms.
    server.limit = 120
    _run_adaptive(clock, server, limiter, duration=600.0)
    assert server.throttled <= 2
    assert server.ok <= 600 * 5 + 120 * 5


def test_adaptive_beats_fixed_rate_baseline_utilization():
    duration, limit, window = 600.0, 600, 60.0
    # Baseline: fixed conservative rate (as configured by hand).
    clock = FakeClock()
    baseline_server = FakeHorizon(clock, limit, window)
    bucket_rate = 5.0
    sent = 0
    while clock() < duration:
        baseline_server.request()
        sent += 1
        clock.now += 1.0 / bucket_rate
    baseline_utilization = baseline_server.ok / (limit * duration / window)
    # Adaptive limiter starting from the same configured rate.
    clock = FakeClock()
    server = FakeHorizon(clock, limit, window)
    limiter = HorizonAdaptiveRateLimiter(rate=bucket_rate, clock=clock, sleep=clock.sleep)
    _run_adaptive(clock, server, limiter, duration)
    adaptive_utilization = server.ok / (limit * duration / window)
    assert adaptive_utilization > baseline_utilization
    assert adaptive_utilization > 0.8
    assert server.throttled == 0


def test_existing_token_bucket_unchanged():
    assert TokenBucket(rate=2.0).capacity == 4.0
