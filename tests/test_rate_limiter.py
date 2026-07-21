"""Tests for the distributed per-key rate limiter (detection/rate_limiter.py).

Covers the acceptance criteria from the distributed-rate-limiting fix:

- Atomic, Redis-backed sliding-window-counter correctness (single process).
- Bounded overshoot when multiple independent OS processes ("replicas")
  share one backing store, refuting the old configured_limit x N_replicas
  bypass.
- REST + gRPC drawing from the same shared quota within one process,
  refuting the old ~2x same-process cross-protocol bypass.
- Fail-open degradation when Redis is unavailable: a logged warning, a
  Prometheus counter increment, and continued (locally bounded) enforcement
  rather than an unbounded or crashing request path.
- The added per-check latency versus the pre-fix in-process-only path.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fakeredis")
pytest.importorskip("lupa")
import fakeredis  # noqa: E402

from detection.rate_limiter import (  # noqa: E402
    DistributedRateLimiter,
    _bucket_keys,
    check_rate_limit,
    get_rate_limiter,
    reset_rate_limiter,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Every test gets a fresh module-level limiter singleton."""
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _fake_client():
    return fakeredis.FakeStrictRedis()


# ---------------------------------------------------------------------------
# Single-process correctness (Redis-backed)
# ---------------------------------------------------------------------------


def test_allows_up_to_limit_then_denies():
    with patch("redis.from_url", return_value=_fake_client()):
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")
    assert limiter.is_using_redis

    allowed_count = 0
    for _ in range(5):
        allowed, retry_after = limiter.check("key-a", 3)
        if allowed:
            allowed_count += 1
        else:
            assert retry_after > 0
    assert allowed_count == 3


def test_limit_zero_or_negative_is_unlimited():
    with patch("redis.from_url", return_value=_fake_client()):
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")
    for _ in range(50):
        allowed, retry_after = limiter.check("key-unlimited", 0)
        assert allowed is True
        assert retry_after == 0


def test_different_keys_have_independent_budgets():
    with patch("redis.from_url", return_value=_fake_client()):
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")
    for _ in range(2):
        assert limiter.check("key-x", 2)[0] is True
    assert limiter.check("key-x", 2)[0] is False
    # A different key_id must not be affected by key-x's exhausted budget.
    assert limiter.check("key-y", 2)[0] is True


def test_bucket_keys_are_hash_tagged_for_cluster_safety():
    cur, prev = _bucket_keys("mykey", 60.0, 123.0)
    assert cur.startswith("ll:ratelimit:{mykey}:")
    assert prev.startswith("ll:ratelimit:{mykey}:")


def test_sliding_window_counter_atomic_under_concurrency():
    """N threads hammering one fakeredis-backed limiter must never let the
    allowed count exceed the configured limit -- the classic INCR/EXPIRE
    TOCTOU race this design avoids via a single atomic Lua script."""
    with patch("redis.from_url", return_value=_fake_client()):
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")

    limit = 20
    allowed_flags = []
    lock = threading.Lock()

    def worker():
        allowed, _ = limiter.check("hot-key", limit)
        with lock:
            allowed_flags.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(allowed_flags) == limit


# ---------------------------------------------------------------------------
# Fail-open fallback when Redis is unavailable
# ---------------------------------------------------------------------------


def test_falls_back_to_local_when_redis_connect_fails(caplog):
    with patch("redis.from_url", side_effect=ConnectionError("refused")):
        with caplog.at_level(logging.WARNING, logger="ledgerlens.rate_limiter"):
            limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")

    assert not limiter.is_using_redis
    assert "local-fallback mode" in caplog.text

    # Still enforces -- just per-process, not distributed.
    allowed_count = sum(limiter.check("k", 3)[0] for _ in range(5))
    assert allowed_count == 3


def test_falls_back_and_emits_metric_when_redis_call_raises_after_connect():
    """Redis connects fine at startup but then starts erroring on every call
    (e.g. network partition mid-flight) -- the circuit must open after
    RATE_LIMITER_FAILURE_THRESHOLD failures and subsequent checks must be
    served locally, with the fallback metric incrementing every time."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    with patch("redis.from_url", return_value=mock_client):
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0")
    assert limiter.is_using_redis

    # register_script wraps the client; force the *script call* to raise.
    limiter._script = MagicMock(side_effect=ConnectionError("timeout"))

    from api.metrics import ledgerlens_rate_limiter_fallback_total

    before = ledgerlens_rate_limiter_fallback_total._value.get()

    from detection.rate_limiter import RATE_LIMITER_FAILURE_THRESHOLD

    for _ in range(RATE_LIMITER_FAILURE_THRESHOLD):
        limiter.check("k", 100)  # each call fails over to local and still succeeds (limit=100)

    assert limiter.circuit_state == "open"

    after = ledgerlens_rate_limiter_fallback_total._value.get()
    assert after - before == RATE_LIMITER_FAILURE_THRESHOLD

    # While open, no further attempts are made against the (still-failing) script.
    call_count_before = limiter._script.call_count
    limiter.check("k", 100)
    assert limiter._script.call_count == call_count_before  # short-circuited, not retried


def test_quota_store_sqlite_never_touches_redis():
    """The explicit opt-out (GATEWAY_QUOTA_STORE=sqlite) must never attempt a
    Redis connection at all -- for single-process/dev/test deployments that
    want zero Redis dependency."""
    with patch("redis.from_url") as mock_from_url:
        limiter = DistributedRateLimiter(redis_url="redis://localhost:6379/0", quota_store="sqlite")
        assert not limiter.is_using_redis
        mock_from_url.assert_not_called()
        allowed, _ = limiter.check("k", 1)
        assert allowed is True
        mock_from_url.assert_not_called()


# ---------------------------------------------------------------------------
# check_rate_limit() / detection.api_key_store.check_rate_limit delegation
# ---------------------------------------------------------------------------


def test_module_level_check_rate_limit_uses_singleton():
    with patch("redis.from_url", return_value=_fake_client()):
        allowed_count = sum(check_rate_limit("singleton-key", 3)[0] for _ in range(5))
    assert allowed_count == 3
    # Confirms get_rate_limiter() actually created and reused one instance.
    assert get_rate_limiter() is get_rate_limiter()


def test_api_key_store_check_rate_limit_delegates_to_shared_limiter():
    """detection.api_key_store.check_rate_limit (imported by both
    api/grpc_scoring_service.py and api/api_key_router.py) must be the same
    shared function -- not a second, independent counter."""
    import detection.api_key_store as store

    with patch("redis.from_url", return_value=_fake_client()):
        reset_rate_limiter()
        allowed_count = sum(store.check_rate_limit("store-key", 3)[0] for _ in range(5))
    assert allowed_count == 3


# ---------------------------------------------------------------------------
# Acceptance criterion: two independent OS processes ("replicas") sharing
# one backing store must not let combined throughput grossly exceed the
# configured per-key limit.
# ---------------------------------------------------------------------------


def _replica_worker(
    redis_url: str, key_id: str, limit: int, attempts: int, quota_store: str, out_queue
) -> None:
    """Runs in its own OS process (spawned fresh, no inherited state) --
    simulates one API replica pod. `quota_store="redis"` exercises the fix;
    `quota_store="sqlite"` exercises the pre-fix local-only behavior as a
    baseline (see test_two_replica_processes_single_process_baseline_would_have_doubled).
    """
    import config.settings as settings_module
    from detection.rate_limiter import check_rate_limit, reset_rate_limiter

    object.__setattr__(settings_module.settings, "redis_url", redis_url)
    object.__setattr__(settings_module.settings, "gateway_quota_store", quota_store)
    reset_rate_limiter()

    allowed = 0
    for _ in range(attempts):
        ok, _ = check_rate_limit(key_id, limit)
        if ok:
            allowed += 1
    out_queue.put(allowed)


@pytest.fixture
def tcp_fake_redis():
    """A real TCP-listening fake Redis server -- reachable from separate OS
    processes exactly like a real Redis instance, unlike an in-memory
    fakeredis.FakeStrictRedis() (which only exists inside one process)."""
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"redis://{host}:{port}/0"
    finally:
        server.shutdown()


def test_two_replica_processes_share_one_effective_quota(tcp_fake_redis):
    """Reproduces the deployment topology from the issue: two independent API
    replicas (separate OS processes, no shared memory) enforcing the same
    per-key limit against a shared Redis. Pre-fix, each process's in-process
    dict let the key through up to `limit` requests *per process*, so two
    replicas combined served ~2x the configured limit. Post-fix, both
    processes consult the same atomic Redis counter, so combined throughput
    must stay within the sliding-window-counter's documented bound (<= 2x
    the limit in the theoretical worst case at a window boundary; for a fast
    burst comfortably inside a single 60s window, as here, it must be exact).
    """
    limit = 10
    attempts_per_replica = 25
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()

    procs = [
        ctx.Process(
            target=_replica_worker,
            args=(tcp_fake_redis, "shared-key-across-replicas", limit, attempts_per_replica, "redis", q),
        )
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    results = [q.get(timeout=5) for _ in procs]
    total_allowed = sum(results)

    # Old (broken) behavior would allow up to `limit` PER replica => 2 * limit.
    # Fixed behavior: combined throughput must not exceed the configured
    # limit for a burst within a single window.
    assert total_allowed == limit, (
        f"combined allowed={total_allowed} across {len(procs)} replicas, "
        f"configured limit={limit} -- replicas are not sharing state"
    )


def test_two_replica_processes_single_process_baseline_would_have_doubled(tcp_fake_redis):
    """Sanity check that the test harness itself would actually detect the
    old bug: with GATEWAY_QUOTA_STORE forced to "sqlite" (the pre-fix,
    local-only mode) in both replicas, combined throughput DOES reach
    ~2x the limit -- confirming test_two_replica_processes_share_one_effective_quota
    is a meaningful regression test, not a tautology."""
    limit = 10
    attempts_per_replica = 25
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()

    procs = [
        ctx.Process(
            target=_replica_worker,
            args=(tcp_fake_redis, "shared-key-local-only", limit, attempts_per_replica, "sqlite", q),
        )
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    total_allowed = sum(q.get(timeout=5) for _ in procs)
    assert total_allowed == limit * len(procs)


# ---------------------------------------------------------------------------
# Latency: added overhead of the Redis round trip vs. the pre-fix local-only path
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_redis_backed_check_latency_within_budget(tcp_fake_redis):
    """Measures real added latency: a genuine loopback TCP round trip to a
    (fake but wire-protocol-real) Redis server, not an in-memory call.
    LedgerLens's documented SLO (docs/slo.md) is 99% of scoring requests
    under 2.0s; this asserts the added per-check cost is a tiny fraction of
    that budget, and reports the measured numbers for the PR record.
    """
    import config.settings as settings_module

    original_redis_url = settings_module.settings.redis_url
    original_quota_store = settings_module.settings.gateway_quota_store
    try:
        object.__setattr__(settings_module.settings, "redis_url", tcp_fake_redis)
        object.__setattr__(settings_module.settings, "gateway_quota_store", "redis")
        reset_rate_limiter()
        limiter = get_rate_limiter()
        assert limiter.is_using_redis

        n = 200
        start = time.perf_counter()
        for i in range(n):
            limiter.check(f"latency-key-{i}", 1_000_000)
        redis_elapsed = time.perf_counter() - start
        redis_per_call_ms = (redis_elapsed / n) * 1000
    finally:
        object.__setattr__(settings_module.settings, "redis_url", original_redis_url)
        object.__setattr__(settings_module.settings, "gateway_quota_store", original_quota_store)
        reset_rate_limiter()

    local = DistributedRateLimiter(redis_url=None, quota_store="sqlite")
    start = time.perf_counter()
    for i in range(n):
        local.check(f"latency-key-{i}", 1_000_000)
    local_elapsed = time.perf_counter() - start
    local_per_call_ms = (local_elapsed / n) * 1000

    added_ms = redis_per_call_ms - local_per_call_ms
    print(
        f"\nrate limiter latency: local={local_per_call_ms:.3f}ms/call "
        f"redis(loopback fakeredis)={redis_per_call_ms:.3f}ms/call "
        f"added={added_ms:.3f}ms/call"
    )

    # Generous ceiling: real production Redis (same-AZ) is typically
    # sub-millisecond; this loopback fakeredis-over-TCP path is a reasonable
    # upper bound proxy. 50ms/call would still be ~2.5% of the 2.0s SLO
    # budget, so this is intentionally loose -- the point is "not
    # pathological", not a tight perf gate.
    assert redis_per_call_ms < 50.0
