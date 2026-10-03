"""Latency benchmark for the streaming-features hot path.

Measures per-trade latency of :meth:`StreamingFeatureEngine.update` (the
function called for every trade on the real-time SSE path) and compares the
p50/p99 against a committed baseline, exiting non-zero on regression.

Usage::

    python3 benchmarks/benchmark_streaming_features.py                    # compare
    python3 benchmarks/benchmark_streaming_features.py --update-baseline  # rewrite baseline
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detection.streaming_features import StreamingFeatureEngine
from ingestion.data_models import Asset, Trade

BASELINE_PATH = Path(__file__).with_name("streaming_features_baseline.json")
N_TRADES = 20_000
N_WALLETS = 200
WARMUP = 2_000
# A run fails if p99 exceeds the baseline by more than this factor.  The
# margin absorbs machine-to-machine noise while still catching reintroduced
# blocking calls, which cost milliseconds rather than microseconds.
REGRESSION_TOLERANCE = 2.0


def _trades(n: int, seed: int = 42) -> list[Trade]:
    rng = random.Random(seed)
    wallets = [f"G{i:055d}" for i in range(N_WALLETS)]
    xlm, usdc = Asset(code="XLM"), Asset(code="USDC", issuer="GISSUER")
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    trades = []
    for i in range(n):
        base, counter = rng.sample(wallets, 2)
        amount = round(rng.lognormvariate(3, 1.5), 7)
        trades.append(Trade(
            id=f"bench-{i}",
            ledger_close_time=start + timedelta(seconds=i * 7),
            base_account=base,
            counter_account=counter,
            base_asset=xlm,
            counter_asset=usdc,
            base_amount=amount,
            counter_amount=amount * 2,
            price=2.0,
            base_is_seller=bool(i % 2),
        ))
    return trades


def measure(n_trades: int = N_TRADES) -> dict[str, float]:
    """Return per-``update`` latency percentiles in microseconds."""
    engine = StreamingFeatureEngine()
    trades = _trades(n_trades + WARMUP)
    for trade in trades[:WARMUP]:
        engine.update(trade)

    samples = []
    for trade in trades[WARMUP:]:
        t0 = time.perf_counter_ns()
        engine.update(trade)
        samples.append((time.perf_counter_ns() - t0) / 1000.0)

    quantiles = statistics.quantiles(samples, n=100)
    return {
        "p50_us": round(quantiles[49], 2),
        "p99_us": round(quantiles[98], 2),
        "mean_us": round(statistics.fmean(samples), 2),
        "n_trades": n_trades,
    }


def check_regression(current: dict[str, float], baseline: dict[str, float]) -> list[str]:
    """Return human-readable failures where ``current`` regresses past tolerance."""
    failures = []
    for key in ("p50_us", "p99_us"):
        limit = baseline[key] * REGRESSION_TOLERANCE
        if current[key] > limit:
            failures.append(f"{key} {current[key]:.1f}us exceeds {REGRESSION_TOLERANCE}x baseline ({limit:.1f}us)")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    result = measure()
    print(json.dumps(result, indent=2))

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(result, indent=2) + "\n")
        print(f"baseline written to {BASELINE_PATH}")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    failures = check_regression(result, baseline)
    for failure in failures:
        print(f"REGRESSION: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
