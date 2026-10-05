"""Pooled vs. per-request Horizon connection benchmark.

Compares the previous behaviour (a fresh connection for every request) with
``AsyncHorizonClient``'s persistent pool under sustained concurrent polling,
and reports latency, throughput and connection reuse rate.

    python -m benchmarks.benchmark_horizon_pool --url https://horizon-testnet.stellar.org --requests 200

Target: pooled reuse rate >= 0.95 and lower p50 latency than per-request.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

from ingestion.http_client import AsyncHorizonClient
from ingestion.metrics import get_metrics


async def _per_request(url: str, n: int, concurrency: int) -> list[float]:
    sem = asyncio.Semaphore(concurrency)

    async def one() -> float:
        async with sem:
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=30.0) as client:
                (await client.get(url)).raise_for_status()
            return time.perf_counter() - started

    return list(await asyncio.gather(*(one() for _ in range(n))))


async def _pooled(url: str, n: int, concurrency: int) -> list[float]:
    async with AsyncHorizonClient(
        url,
        max_concurrency=concurrency,
        version_guard=None,
        rate_limit_rps=1000.0,
        rate_burst=1000.0,
    ) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one() -> float:
            async with sem:
                started = time.perf_counter()
                await client.get(url)
                return time.perf_counter() - started

        return list(await asyncio.gather(*(one() for _ in range(n))))


def _report(label: str, latencies: list[float], wall: float) -> None:
    ordered = sorted(latencies)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(
        f"{label:<12} p50={statistics.median(ordered) * 1000:7.1f}ms "
        f"p95={p95 * 1000:7.1f}ms throughput={len(ordered) / wall:7.1f} req/s"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://horizon-testnet.stellar.org")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()

    for label, runner in (("per-request", _per_request), ("pooled", _pooled)):
        started = time.perf_counter()
        latencies = await runner(args.url, args.requests, args.concurrency)
        _report(label, latencies, time.perf_counter() - started)

    metrics = get_metrics()
    try:
        opened = metrics.http_connections_opened_total._value.get()
        reused = metrics.http_connections_reused_total._value.get()
        print(f"pooled reuse rate={reused / max(opened + reused, 1):.3f}")
    except AttributeError:
        print("prometheus_client not installed; reuse rate unavailable")


if __name__ == "__main__":
    asyncio.run(main())
