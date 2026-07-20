"""
Benchmark: Numba JIT vs pure-Python for feature_engineering.py hot loops.

Run: python benchmarks/benchmark_feature_engineering.py
"""
import time
import numpy as np
import pandas as pd

from config.settings import settings
from detection.feature_engineering import round_trip_trade_frequency, cross_pair_features

SCALES = [1_000, 10_000, 50_000]


def make_synthetic_trades(n: int) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    symbols = [f"ASSET{i}" for i in range(20)]
    pairs = [f"PAIR{i}" for i in range(5)]
    return pd.DataFrame({
        "account": ["acct_0"] * n,
        "gave": rng.choice(symbols, n),
        "got": rng.choice(symbols, n),
        "pair": rng.choice(pairs, n),
        "timestamp_us": np.sort(rng.integers(0, 10**9, n)).astype(np.int64),
    })


def time_it(fn, *args, **kwargs) -> float:
    start = time.perf_counter()
    fn(*args, **kwargs)
    return time.perf_counter() - start


def run():
    for n in SCALES:
        trades = make_synthetic_trades(n)
        print(f"\n=== n={n} trades ===")

        for jit_enabled in (True, False):
            settings.feature_engine_jit_enabled = jit_enabled
            # warm-up call for JIT compilation, excluded from timing
            round_trip_trade_frequency(trades, "acct_0")

            t_round_trip = time_it(round_trip_trade_frequency, trades, "acct_0")
            t_cross_pair = time_it(cross_pair_features, trades, "acct_0")

            label = "JIT" if jit_enabled else "Python"
            print(f"  [{label}] round_trip_trade_frequency: {t_round_trip*1000:.2f}ms")
            print(f"  [{label}] cross_pair_features:        {t_cross_pair*1000:.2f}ms")


if __name__ == "__main__":
    run()