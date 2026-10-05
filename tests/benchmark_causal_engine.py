"""Benchmark for causal-inference PDC: per-item sync vs batched vs async.

Run: ``python -m tests.benchmark_causal_engine``
"""

import asyncio
import cProfile
import pstats
import time

import numpy as np
import pandas as pd

from detection.causal_engine import estimate_pdc, estimate_pdc_batch, estimate_pdc_batch_async

PAIR = "XLM/USDC"


def make_data(n_wallets: int = 50, n_trades: int = 5000, hours: int = 24, seed: int = 0):
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-01", tz="UTC")
    wallets = [f"G{i:055d}" for i in range(n_wallets)]
    ts = start + pd.to_timedelta(rng.uniform(0, hours * 3600, n_trades), unit="s")
    trades = pd.DataFrame(
        {
            "ledger_close_time": ts,
            "asset_pair": PAIR,
            "base_account": rng.choice(wallets, n_trades),
            "counter_account": rng.choice(wallets, n_trades),
            "base_amount": rng.lognormal(3, 1, n_trades),
        }
    )
    price_ts = pd.date_range(start, periods=hours * 60, freq="1min")
    prices = pd.DataFrame({"timestamp": price_ts, "mid_price": 0.1 + rng.normal(0, 1e-4, len(price_ts)).cumsum()})
    return trades, prices, wallets


def bench_sync(trades, prices, wallets) -> float:
    t0 = time.perf_counter()
    for w in wallets:
        estimate_pdc(trades, prices, w, PAIR)
    return time.perf_counter() - t0


def bench_batch(trades, prices, wallets) -> float:
    t0 = time.perf_counter()
    estimate_pdc_batch(trades, prices, wallets, PAIR)
    return time.perf_counter() - t0


async def bench_async_loop_latency(trades, prices, wallets) -> tuple[float, float]:
    """Return (batch wall time, max event-loop stall) while PDC runs off-loop."""
    stalls: list[float] = []
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            stalls.append(time.perf_counter() - t - 0.005)

    tick = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    await estimate_pdc_batch_async(trades, prices, wallets, PAIR)
    elapsed = time.perf_counter() - t0
    done.set()
    await tick
    return elapsed, max(stalls, default=0.0)


def main() -> None:
    trades, prices, wallets = make_data()

    profiler = cProfile.Profile()
    profiler.enable()
    estimate_pdc(trades, prices, wallets[0], PAIR)
    profiler.disable()
    print("Top cumulative cost for one estimate_pdc call:")
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(12)

    sync_s = bench_sync(trades, prices, wallets)
    batch_s = bench_batch(trades, prices, wallets)
    async_s, stall = asyncio.run(bench_async_loop_latency(trades, prices, wallets))
    n = len(wallets)
    print(f"sync per-item : {sync_s:.3f}s ({n / sync_s:.1f} wallets/s)")
    print(f"batched       : {batch_s:.3f}s ({n / batch_s:.1f} wallets/s, {sync_s / batch_s:.2f}x)")
    print(f"async batched : {async_s:.3f}s, max event-loop stall {stall * 1000:.1f}ms")


if __name__ == "__main__":
    main()
