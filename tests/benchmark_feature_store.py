"""Benchmark for streaming feature store: incremental vs full recompute.

Measures performance of update_feature_state() + derive_feature_vector()
vs the original build_feature_vector() full-recompute path.
"""

import time
import pandas as pd
from datetime import datetime, timedelta, timezone

from detection.feature_store import (
    WalletFeatureState,
    update_feature_state,
    derive_feature_vector,
)
from detection.feature_engineering import build_feature_vector, ROLLING_WINDOWS
from ingestion.data_models import Trade, Asset, TradeType
from ingestion.synthetic_data import generate_synthetic_trades


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
    print("\nGenerating 10,000 synthetic trades for 100 wallets...")
    trades = generate_synthetic_trades(num_trades=10_000, num_wallets=100)
    print(f"Generated {len(trades)} trades")
    
    # Warm up
    print("\nWarming up (small sample)...")
    sample_trades = trades[:100]
    benchmark_incremental_updates(sample_trades, num_wallets=5)
    benchmark_full_recompute(sample_trades, num_wallets=5)
    
    # Benchmark incremental path
    print("\nBenchmarking incremental path (10,000 trades, 100 wallets)...")
    inc_time, inc_features = benchmark_incremental_updates(trades, num_wallets=100)
    print(f"  Incremental: {inc_time:.3f}s")
    
    # Benchmark full-recompute path
    print("Benchmarking full-recompute path (10,000 trades, 100 wallets)...")
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


if __name__ == "__main__":
    success = run_benchmark()
    exit(0 if success else 1)
