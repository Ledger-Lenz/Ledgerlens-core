#!/usr/bin/env python3
"""Overhead benchmark for :class:`detection.rate_limiter.DistributedRateLimiter`.

Measures p50/p95/p99 latency of one ``check()`` call and aggregate
throughput under concurrent load (N threads sharing one limiter, as request
handlers in one replica do), for each backend:

- ``redis``: the distributed path. Uses a real Redis when ``--redis-url`` /
  ``REDIS_URL`` is set (the number that matters for production); otherwise
  an in-process ``fakeredis`` server, which measures limiter + Lua-script
  overhead without network latency.
- ``local``: the fail-open fallback path used while Redis is unreachable.

Budgets (per check, p99): ``redis`` < 5ms, ``local`` < 0.5ms. A rate limiter
that costs more than a few ms per request would dominate the scoring
pipeline's 50ms p99 target (see ``benchmarks/benchmark_scoring.py``).

Usage
-----
    python3 benchmarks/benchmark_rate_limiter.py
    REDIS_URL=redis://localhost:6379/0 python3 benchmarks/benchmark_rate_limiter.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detection.rate_limiter import DistributedRateLimiter  # noqa: E402

P99_BUDGET_MS = {"redis": 5.0, "local": 0.5}
_HIGH_LIMIT = 10**9  # never deny, so every call exercises the full write path


def _make_limiter(backend: str, redis_url: str | None) -> DistributedRateLimiter:
    if backend == "local":
        return DistributedRateLimiter(quota_store="sqlite")
    if redis_url:
        limiter = DistributedRateLimiter(redis_url=redis_url, quota_store="redis")
    else:
        import fakeredis

        server = fakeredis.FakeServer()
        with patch("redis.from_url", lambda *a, **k: fakeredis.FakeStrictRedis(server=server)):
            limiter = DistributedRateLimiter(redis_url="redis://fake", quota_store="redis")
    if not limiter.is_using_redis:
        raise RuntimeError("Redis backend requested but limiter fell back to local mode")
    return limiter


def _percentile(samples: list[float], pct: float) -> float:
    return statistics.quantiles(samples, n=100)[int(pct) - 1] if len(samples) > 1 else samples[0]


def run(
    backend: str, iterations: int = 2000, threads: int = 8, redis_url: str | None = None
) -> dict:
    """Benchmark one backend and return latency/throughput stats (ms, ops/s)."""
    limiter = _make_limiter(backend, redis_url)
    for i in range(100):  # warm up (connection pool, script cache)
        limiter.check(f"warmup-{i % 4}", _HIGH_LIMIT)

    latencies: list[float] = []
    for i in range(iterations):
        start = time.perf_counter()
        limiter.check(f"bench-key-{i % 16}", _HIGH_LIMIT)
        latencies.append((time.perf_counter() - start) * 1000)

    per_thread = max(1, iterations // threads)

    def worker(tid: int) -> None:
        for i in range(per_thread):
            limiter.check(f"bench-load-{(tid + i) % 16}", _HIGH_LIMIT)

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    start = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    elapsed = time.perf_counter() - start

    return {
        "backend": backend,
        "real_redis": bool(redis_url) if backend == "redis" else None,
        "p50_ms": round(_percentile(latencies, 50), 4),
        "p95_ms": round(_percentile(latencies, 95), 4),
        "p99_ms": round(_percentile(latencies, 99), 4),
        "p99_budget_ms": P99_BUDGET_MS[backend],
        "throughput_ops_per_s": round(per_thread * threads / elapsed, 1),
        "threads": threads,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL"))
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    failed = False
    for backend in ("redis", "local"):
        result = run(backend, args.iterations, args.threads, args.redis_url)
        print(json.dumps(result))
        if result["p99_ms"] > result["p99_budget_ms"]:
            print(f"FAIL: {backend} p99 exceeds budget", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
