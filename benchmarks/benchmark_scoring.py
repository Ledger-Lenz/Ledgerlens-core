#!/usr/bin/env python3
"""Performance benchmarks for the LedgerLens scoring pipeline.

Measures p50/p95/p99 latency for three scenarios:

- ``single_wallet_score``: scoring one pre-computed feature vector
  (target: p99 < 50ms).
- ``feature_extraction_1000_trades``: extracting one wallet's feature
  vector from a 1000-trade batch (target: p99 < 200ms).
- ``batch_scoring_100_wallets``: scoring 100 pre-computed feature vectors
  in a single vectorized call (target: per-wallet cost stays roughly flat
  vs. the single-wallet baseline -- i.e. scales linearly with wallet count,
  not worse).

Trade data comes from ``tests.factories.TradeFactory`` with a pinned seed,
and models are small in-process ``RandomForestClassifier``s (mirroring the
fixture in ``tests/test_model_inference.py``) so this runs reproducibly in
CI without needing pre-trained model artifacts or real xgboost/lightgbm
installs.

This intentionally does **not** use ``pytest-benchmark``: it isn't a
project dependency (see ``requirements.txt``), and percentiles computed
from a fixed-size warm sample via stdlib ``time.perf_counter`` +
``statistics`` are precise enough for CI regression detection without
adding one.

Usage
-----
    python3 benchmarks/benchmark_scoring.py                 # compare to baseline.json
    python3 benchmarks/benchmark_scoring.py --update-baseline   # (re)write baseline.json
    make benchmark                                            # same as --update-baseline
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

SEED = 42
BENCHMARKS_DIR = Path(__file__).resolve().parent
BASELINE_PATH = BENCHMARKS_DIR / "baseline.json"
REGRESSION_THRESHOLD = 0.20  # CI fails if measured p99 exceeds baseline p99 by more than this
WARMUP_ITERATIONS = 5

TARGET_P99_MS = {
    "single_wallet_score": 50.0,
    "feature_extraction_1000_trades": 200.0,
}


def _make_models():
    """Three small `RandomForestClassifier`s standing in for
    random_forest/xgboost/lightgbm -- mirrors the fixture in
    `tests/test_model_inference.py` so this benchmark exercises the real
    ensemble-scoring code path without needing pre-trained artifacts or
    the xgboost/lightgbm packages installed.
    """
    from sklearn.ensemble import RandomForestClassifier

    from detection.feature_engineering import FEATURE_NAMES

    X = [[0] * len(FEATURE_NAMES), [1] * len(FEATURE_NAMES)]
    y = [0, 1]
    return {
        name: RandomForestClassifier(n_estimators=5, random_state=SEED).fit(X, y)
        for name in ("random_forest", "xgboost", "lightgbm")
    }


def _percentiles(samples: list[float]) -> dict:
    """`samples` are durations in seconds; returned values are in ms."""
    ordered = sorted(samples)

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
        return ordered[idx]

    return {
        "p50_ms": round(pct(50) * 1000, 4),
        "p95_ms": round(pct(95) * 1000, 4),
        "p99_ms": round(pct(99) * 1000, 4),
        "mean_ms": round(statistics.mean(ordered) * 1000, 4),
        "samples": len(ordered),
    }


def _time_calls(fn, iterations: int) -> list[float]:
    for _ in range(WARMUP_ITERATIONS):
        fn()
    durations = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        durations.append(time.perf_counter() - start)
    return durations


def benchmark_single_wallet_score(models: dict, iterations: int = 200) -> dict:
    """Score one pre-computed feature vector at a time. Target: p99 < 50ms."""
    from detection.feature_engineering import FEATURE_NAMES
    from detection.model_inference import score_feature_vector

    feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)
    durations = _time_calls(lambda: score_feature_vector(models, feature_vector), iterations)
    return _percentiles(durations)


def benchmark_feature_extraction(iterations: int = 20) -> dict:
    """Extract one wallet's feature vector from a fixed 1000-trade batch.
    Target: p99 < 200ms."""
    import pandas as pd

    from detection.feature_engineering import build_feature_vector
    from tests.factories import TradeFactory

    trades = TradeFactory.legitimate_market_maker(n_trades=1000, seed=SEED)
    trades_df = pd.DataFrame([t.model_dump() for t in trades])
    account = trades[0].base_account
    as_of = pd.Timestamp(trades_df["ledger_close_time"].max())

    durations = _time_calls(lambda: build_feature_vector(trades_df, account, as_of), iterations)
    return _percentiles(durations)


def benchmark_batch_scoring(models: dict, n_wallets: int = 100, iterations: int = 30) -> dict:
    """Score `n_wallets` pre-computed feature vectors in one vectorized call.

    Target: linear scaling from the single-wallet baseline, i.e.
    per-wallet cost (`p50_ms / n_wallets`) shouldn't be worse than the
    single-wallet `p50_ms` -- vectorized batch scoring should be at *least*
    as cheap per wallet, not worse.
    """
    from detection.feature_engineering import FEATURE_NAMES
    from detection.model_inference import _score_feature_matrix_base

    feature_vectors = [dict.fromkeys(FEATURE_NAMES, 1.0) for _ in range(n_wallets)]
    durations = _time_calls(lambda: _score_feature_matrix_base(models, feature_vectors), iterations)
    result = _percentiles(durations)
    result["n_wallets"] = n_wallets
    result["per_wallet_p50_ms"] = round(result["p50_ms"] / n_wallets, 4)
    return result


def _hardware_fingerprint() -> dict:
    """CPU/RAM/platform info so baseline numbers can be sanity-checked
    across machines, not treated as universal absolutes."""
    import os

    cpu = platform.processor() or platform.machine()
    ram_gb = None
    try:
        ram_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        ram_gb = round(ram_bytes / (1024**3), 1)
    except (ValueError, AttributeError, OSError):
        pass
    return {
        "cpu": cpu,
        "ram_gb": ram_gb,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


def run_all() -> dict:
    models = _make_models()
    scenarios = {
        "single_wallet_score": benchmark_single_wallet_score(models),
        "feature_extraction_1000_trades": benchmark_feature_extraction(),
        "batch_scoring_100_wallets": benchmark_batch_scoring(models),
    }
    return {
        "seed": SEED,
        "hardware": _hardware_fingerprint(),
        "scenarios": scenarios,
    }


def _load_baseline() -> dict | None:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text())
    return None


def check_regressions(results: dict, baseline: dict, threshold: float = REGRESSION_THRESHOLD) -> list[str]:
    """Compare `results["scenarios"][name]["p99_ms"]` against the matching
    baseline entry for every scenario present in both. Returns a list of
    human-readable failure messages (empty if no regression exceeds
    `threshold`). Pure stdlib / no project dependencies -- exercised
    directly by `tests/test_benchmark_regression_check.py`.
    """
    failures = []
    for name, scenario in results.get("scenarios", {}).items():
        base_scenario = baseline.get("scenarios", {}).get(name)
        if not base_scenario:
            continue
        base_p99 = base_scenario.get("p99_ms")
        new_p99 = scenario.get("p99_ms")
        if not base_p99 or base_p99 <= 0:
            continue
        regression = (new_p99 - base_p99) / base_p99
        if regression > threshold:
            failures.append(
                f"{name}: p99 regressed {regression:.1%} "
                f"(baseline={base_p99}ms, measured={new_p99}ms, threshold={threshold:.0%})"
            )
    return failures


def check_linear_scaling(results: dict, tolerance: float = 0.5) -> list[str]:
    """Sanity-check that batch-scoring's per-wallet cost isn't worse than
    the single-wallet baseline by more than `tolerance` (50% by default --
    batch scoring is vectorized and should be cheaper per wallet, not
    dramatically more expensive)."""
    scenarios = results.get("scenarios", {})
    single = scenarios.get("single_wallet_score")
    batch = scenarios.get("batch_scoring_100_wallets")
    if not single or not batch:
        return []
    single_p50 = single["p50_ms"]
    per_wallet_p50 = batch["per_wallet_p50_ms"]
    if single_p50 <= 0:
        return []
    overage = (per_wallet_p50 - single_p50) / single_p50
    if overage > tolerance:
        return [
            f"batch_scoring_100_wallets: per-wallet cost ({per_wallet_p50}ms) is "
            f"{overage:.1%} higher than the single-wallet baseline ({single_p50}ms), "
            f"exceeding the {tolerance:.0%} linear-scaling tolerance"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write measured results to benchmarks/baseline.json instead of comparing against it.",
    )
    args = parser.parse_args(argv)

    results = run_all()
    print(json.dumps(results, indent=2))

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nWrote baseline to {BASELINE_PATH}")
        return 0

    baseline = _load_baseline()
    if baseline is None:
        # Don't fail CI just because nobody has established a baseline yet --
        # only enforce regressions once benchmarks/baseline.json exists.
        print(
            "\nNo benchmarks/baseline.json found -- skipping regression check. "
            "Run `python3 benchmarks/benchmark_scoring.py --update-baseline` "
            "(or `make benchmark`) once on a reference machine to establish one."
        )
        return 0

    failures = check_regressions(results, baseline) + check_linear_scaling(results)
    if failures:
        print("\nREGRESSIONS DETECTED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nNo regressions detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
