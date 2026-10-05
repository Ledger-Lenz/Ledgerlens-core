"""Benchmark for streaming feature store: incremental vs full recompute.

Measures performance of update_feature_state() + derive_feature_vector()
vs the original build_feature_vector() full-recompute path.

``--gate`` turns it into a performance-regression check (Issue #1040): the
median incremental-path time is compared with a rolling baseline taken from
the tracked history file and the run fails on a regression beyond the
threshold. See benchmarks/README.md ("Feature-store regression gate").
"""

import argparse
import json
import os
import statistics
import sys
import time
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

from detection.feature_store import (
    WalletFeatureState,
    update_feature_state,
    derive_feature_vector,
)
from detection.feature_engineering import build_feature_vector
from ingestion.data_models import Trade
from ingestion.synthetic_data import generate_synthetic_dataset

DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "feature_store_history.json"
REGRESSION_THRESHOLD = 0.25  # fail when >25% slower than the rolling baseline
BASELINE_WINDOW = 5  # rolling baseline = median of the last N recorded runs
GATE_REPEATS = 5


def generate_synthetic_trades(num_wallets: int = 100, seed: int = 42) -> list[Trade]:
    """Deterministic synthetic trade history for ``num_wallets`` normal accounts."""
    trades_df, _, _, _ = generate_synthetic_dataset(
        n_normal_accounts=num_wallets, trades_per_normal=50, seed=seed
    )
    return [Trade(**row) for row in trades_df.to_dict(orient="records")]


def benchmark_incremental_updates(trades: list[Trade], num_wallets: int = 100) -> tuple[float, dict]:
    """Benchmark incremental update path.
    
    Returns (elapsed_time, sample_features).
    """
    # Group trades by wallet
    wallet_trades = {}
    for trade in trades:
        wallet = trade.base_account
        if wallet not in wallet_trades:
            wallet_trades[wallet] = []
        wallet_trades[wallet].append(trade)
    
    # Select sample wallets
    sample_wallets = list(wallet_trades.keys())[:num_wallets]
    
    start = time.perf_counter()
    
    sample_features = {}
    for wallet in sample_wallets:
        wt = wallet_trades.get(wallet, [])
        if not wt:
            continue
        
        asset_pair = wt[0].asset_pair
        state = WalletFeatureState(
            wallet=wallet,
            asset_pair=asset_pair,
            last_updated=datetime.now(timezone.utc),
        )
        
        # Incrementally update state with each trade
        for trade in wt:
            if trade.base_account == wallet or trade.counter_account == wallet:
                state = update_feature_state(state, trade)
        
        # Derive features from cached state
        features = derive_feature_vector(state)
        sample_features[wallet] = features
    
    elapsed = time.perf_counter() - start
    return elapsed, sample_features


def benchmark_full_recompute(trades: list[Trade], num_wallets: int = 100) -> tuple[float, dict]:
    """Benchmark full-recompute path (original behavior).
    
    Returns (elapsed_time, sample_features).
    """
    # Convert to DataFrame like the original pipeline
    trades_df = pd.DataFrame([t.model_dump() for t in trades])
    
    if trades_df.empty:
        return 0.0, {}
    
    # Get unique accounts
    accounts = pd.unique(trades_df[["base_account", "counter_account"]].values.ravel())
    accounts = [a for a in accounts if pd.notna(a)][:num_wallets]
    
    as_of = pd.Timestamp(trades_df["ledger_close_time"].max())
    
    start = time.perf_counter()
    
    sample_features = {}
    for account in accounts:
        # Full recompute for each account
        features = build_feature_vector(trades_df, account, as_of)
        sample_features[account] = features
    
    elapsed = time.perf_counter() - start
    return elapsed, sample_features


def compare_feature_vectors(
    features_incremental: dict,
    features_full: dict,
    tolerance: float = 1e-6,
) -> tuple[bool, list[str]]:
    """Compare incremental vs full features within tolerance.
    
    Returns (all_match, list_of_mismatches).
    """
    mismatches = []
    
    for wallet in features_incremental:
        if wallet not in features_full:
            mismatches.append(f"Wallet {wallet} missing in full features")
            continue
        
        inc_feat = features_incremental[wallet]
        full_feat = features_full[wallet]
        
        for key in inc_feat:
            if key not in full_feat:
                # OK, incremental may not compute all features
                continue
            
            inc_val = inc_feat[key]
            full_val = full_feat[key]
            
            if full_val == 0:
                if inc_val != 0:
                    relative_error = float("inf")
                else:
                    relative_error = 0
            else:
                relative_error = abs(inc_val - full_val) / abs(full_val)
            
            if relative_error > tolerance:
                mismatches.append(
                    f"Wallet {wallet}, feature {key}: "
                    f"incremental={inc_val}, full={full_val}, "
                    f"rel_error={relative_error:.2e}"
                )
    
    return len(mismatches) == 0, mismatches


def run_benchmark():
    """Run the full benchmark suite."""
    print("=" * 80)
    print("Feature Store Benchmark: Incremental vs Full Recompute")
    print("=" * 80)
    
    # Generate synthetic trades
    print("\nGenerating synthetic trades for 100 wallets...")
    trades = generate_synthetic_trades(num_wallets=100)
    print(f"Generated {len(trades)} trades")
    
    # Warm up
    print("\nWarming up (small sample)...")
    sample_trades = trades[:100]
    benchmark_incremental_updates(sample_trades, num_wallets=5)
    benchmark_full_recompute(sample_trades, num_wallets=5)
    
    # Benchmark incremental path
    print("\nBenchmarking incremental path (100 wallets)...")
    inc_time, inc_features = benchmark_incremental_updates(trades, num_wallets=100)
    print(f"  Incremental: {inc_time:.3f}s")
    
    # Benchmark full-recompute path
    print("Benchmarking full-recompute path (100 wallets)...")
    full_time, full_features = benchmark_full_recompute(trades, num_wallets=100)
    print(f"  Full recompute: {full_time:.3f}s")
    
    # Calculate speedup
    speedup = full_time / inc_time if inc_time > 0 else float("inf")
    print(f"\n  Speedup: {speedup:.2f}x")
    
    # Feature equivalence check
    print("\nChecking feature equivalence (tolerance 1e-6)...")
    match, mismatches = compare_feature_vectors(inc_features, full_features, tolerance=1e-6)
    
    if match:
        print("  ✓ All feature values match within 1e-6 tolerance")
    else:
        print(f"  ✗ Found {len(mismatches)} mismatches:")
        for mismatch in mismatches[:5]:
            print(f"    - {mismatch}")
        if len(mismatches) > 5:
            print(f"    ... and {len(mismatches) - 5} more")
    
    # Acceptance criteria
    print("\n" + "=" * 80)
    print("Acceptance Criteria:")
    print("=" * 80)
    
    criteria_met = True
    
    # Criterion 1: 3x speedup
    speedup_ok = speedup >= 3.0
    print(f"✓ Speedup ≥ 3.0x: {speedup:.2f}x {'PASS' if speedup_ok else 'FAIL'}")
    criteria_met = criteria_met and speedup_ok
    
    # Criterion 2: Feature equivalence within 1e-6
    equiv_ok = match
    print(f"✓ Feature equivalence (1e-6): {'PASS' if equiv_ok else 'FAIL'}")
    criteria_met = criteria_met and equiv_ok
    
    print("\n" + ("=" * 80))
    if criteria_met:
        print("BENCHMARK PASSED ✓")
    else:
        print("BENCHMARK FAILED ✗")
    print("=" * 80)
    
    return criteria_met


def _load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _trend_markdown(history: list[dict], limit: int = 20) -> str:
    """Markdown trend table with a text bar chart of recent runs."""
    recent = history[-limit:]
    if not recent:
        return "_No feature-store benchmark history yet._\n"
    peak = max(r["incremental_ms"] for r in recent)
    lines = [
        "### Feature-store benchmark trend (incremental path, median ms)",
        "",
        "| Recorded | Commit | ms | Trend |",
        "|---|---|---:|---|",
    ]
    for r in recent:
        bar = "█" * max(1, round(20 * r["incremental_ms"] / peak))
        lines.append(f"| {r['recorded_at'][:19]} | `{r['commit'][:7]}` | {r['incremental_ms']:.1f} | {bar} |")
    return "\n".join(lines) + "\n"


def run_regression_gate(
    history_path: Path,
    threshold: float = REGRESSION_THRESHOLD,
    window: int = BASELINE_WINDOW,
    record: bool = False,
) -> bool:
    """Compare the current run with the rolling baseline; return True on pass."""
    trades = generate_synthetic_trades(num_wallets=100)
    benchmark_incremental_updates(trades[:200], num_wallets=5)  # warm-up
    samples = [benchmark_incremental_updates(trades, num_wallets=100)[0] for _ in range(GATE_REPEATS)]
    current_ms = statistics.median(samples) * 1000

    history = _load_history(history_path)
    recent = [r["incremental_ms"] for r in history[-window:]]
    passed = True
    if recent:
        baseline_ms = statistics.median(recent)
        change = (current_ms - baseline_ms) / baseline_ms
        passed = change <= threshold
        print(
            f"incremental path: {current_ms:.1f} ms vs baseline {baseline_ms:.1f} ms "
            f"(median of last {len(recent)}): {change:+.1%} "
            f"[threshold +{threshold:.0%}] {'PASS' if passed else 'REGRESSION'}"
        )
    else:
        print(f"incremental path: {current_ms:.1f} ms (no baseline yet — recording first run)")

    if record:
        history.append({
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "commit": os.getenv("GITHUB_SHA", "local"),
            "incremental_ms": round(current_ms, 3),
        })
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history, indent=2) + "\n")

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(_trend_markdown(history))
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="run the regression gate instead of the comparison")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--threshold", type=float, default=REGRESSION_THRESHOLD)
    parser.add_argument("--window", type=int, default=BASELINE_WINDOW)
    parser.add_argument("--record", action="store_true", help="append a passing result to the history file")
    args = parser.parse_args()
    if args.gate:
        success = run_regression_gate(args.history, args.threshold, args.window, args.record)
    else:
        success = run_benchmark()
    sys.exit(0 if success else 1)
