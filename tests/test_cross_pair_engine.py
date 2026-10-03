"""Tests for detection.cross_pair_engine."""

import time

import numpy as np
import pandas as pd
import pytest

from detection.cross_pair_engine import (
    build_volume_time_series,
    find_correlated_pairs,
    find_cross_pair_wallets,
)


def _make_trades(
    pair: str,
    times: list[pd.Timestamp],
    amounts: list[float],
    wallet_a: str = "W1",
    wallet_b: str = "W2",
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ledger_close_time": times,
            "base_account": wallet_a,
            "counter_account": wallet_b,
            "base_amount": amounts,
            "base_asset": [{"code": "XLM", "issuer": None}] * len(times),
            "counter_asset": [{"code": pair, "issuer": "GISSUER"}] * len(times),
        }
    )


def _uniform_times(n: int, start: pd.Timestamp, freq: str = "4h") -> list[pd.Timestamp]:
    return list(pd.date_range(start=start, periods=n, freq=freq, tz="UTC"))


# ---------------------------------------------------------------------------
# build_volume_time_series
# ---------------------------------------------------------------------------


def test_build_volume_time_series_returns_pair_columns():
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=28)
    times = _uniform_times(20, base)
    trades = {
        "XLM/USDC": _make_trades("USDC", times, [10.0] * 20),
        "XLM/AQUA": _make_trades("AQUA", times, [5.0] * 20),
    }
    matrix = build_volume_time_series(trades)
    assert "XLM/USDC" in matrix.columns
    assert "XLM/AQUA" in matrix.columns


def test_build_volume_time_series_empty_input():
    matrix = build_volume_time_series({})
    assert matrix.empty


def test_build_volume_time_series_performance_200_pairs():
    """Volume matrix construction for 200 pairs × 180 buckets must finish under 500 ms."""
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    times = _uniform_times(180, base, freq="4h")
    rng = np.random.default_rng(42)
    trades = {}
    for i in range(200):
        pair = f"PAIR{i}/XLM"
        amounts = rng.uniform(1, 100, size=180).tolist()
        trades[pair] = _make_trades(pair, times, amounts)

    start = time.perf_counter()
    matrix = build_volume_time_series(trades)
    elapsed = time.perf_counter() - start

    assert matrix.shape[1] == 200
    assert elapsed < 0.5, f"Volume matrix took {elapsed:.3f}s (limit 0.5s)"


# ---------------------------------------------------------------------------
# find_correlated_pairs
# ---------------------------------------------------------------------------


def test_find_correlated_pairs_identifies_correlated():
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    times = _uniform_times(30, base)
    rng = np.random.default_rng(0)
    amounts = rng.uniform(10, 100, size=30).tolist()

    # Two pairs with identical volume pattern → r ≈ 1.0
    trades = {
        "XLM/USDC": _make_trades("USDC", times, amounts),
        "XLM/AQUA": _make_trades("AQUA", times, amounts),
    }
    matrix = build_volume_time_series(trades)
    result = find_correlated_pairs(matrix, correlation_threshold=0.75, min_active_buckets=5)

    assert len(result) == 1
    pa, pb, r = result[0]
    assert {pa, pb} == {"XLM/USDC", "XLM/AQUA"}
    assert r > 0.75


def test_find_correlated_pairs_ignores_uncorrelated():
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    times = _uniform_times(30, base)
    rng = np.random.default_rng(1)

    amounts_a = rng.uniform(10, 100, size=30).tolist()
    # Independent draw → near-zero Spearman correlation in expectation
    amounts_b = rng.uniform(10, 100, size=30).tolist()

    trades = {
        "XLM/USDC": _make_trades("USDC", times, amounts_a),
        "XLM/AQUA": _make_trades("AQUA", times, amounts_b),
    }
    matrix = build_volume_time_series(trades)
    result = find_correlated_pairs(matrix, correlation_threshold=0.75, min_active_buckets=5)

    assert result == []


def test_find_correlated_pairs_outlier_does_not_create_false_positive():
    """A single 100× outlier trade must not create a false Spearman correlation
    between otherwise uncorrelated pairs (validates Spearman's outlier robustness)."""
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    times = _uniform_times(30, base)
    rng = np.random.default_rng(2)

    amounts_a = rng.uniform(10, 20, size=30).tolist()
    amounts_b = rng.uniform(10, 20, size=30).tolist()

    # Inject a massive outlier at index 5 in pair A only
    amounts_a[5] = amounts_a[5] * 100

    trades = {
        "XLM/USDC": _make_trades("USDC", times, amounts_a),
        "XLM/AQUA": _make_trades("AQUA", times, amounts_b),
    }
    matrix = build_volume_time_series(trades)
    result = find_correlated_pairs(matrix, correlation_threshold=0.75, min_active_buckets=5)

    assert result == [], (
        "Spearman correlation produced a false positive from a single outlier trade"
    )


def test_find_correlated_pairs_empty_matrix():
    result = find_correlated_pairs(pd.DataFrame())
    assert result == []


def test_find_correlated_pairs_applies_fdr_correction():
    """A correlation must remain significant after correcting all pair tests."""
    rng = np.random.default_rng(123)
    matrix = pd.DataFrame(rng.normal(size=(30, 8)), columns=[f"p{i}" for i in range(8)])
    matrix["p1"] = matrix["p0"]

    result = find_correlated_pairs(
        matrix,
        correlation_threshold=0.75,
        min_active_buckets=1,
        fdr_alpha=0.05,
    )

    assert [(a, b) for a, b, _ in result] == [("p0", "p1")]


def test_find_correlated_pairs_validates_fdr_alpha():
    matrix = pd.DataFrame(np.ones((10, 2)), columns=["a", "b"])
    with pytest.raises(ValueError, match="fdr_alpha"):
        find_correlated_pairs(matrix, fdr_alpha=0.0)


def test_find_correlated_pairs_performance_200_pairs():
    """Correlation computation for 200×200 matrix must finish under 1 second."""
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    times = _uniform_times(180, base, freq="4h")
    rng = np.random.default_rng(99)
    trades = {}
    for i in range(200):
        pair = f"PAIR{i}/XLM"
        amounts = rng.uniform(1, 100, size=180).tolist()
        trades[pair] = _make_trades(pair, times, amounts)

    matrix = build_volume_time_series(trades)

    start = time.perf_counter()
    find_correlated_pairs(matrix, min_active_buckets=5)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Correlation took {elapsed:.3f}s (limit 1.0s)"


# ---------------------------------------------------------------------------
# find_cross_pair_wallets
# ---------------------------------------------------------------------------


def test_find_cross_pair_wallets_detects_synchronised_wallet():
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
    t = base

    df_a = pd.DataFrame(
        {
            "ledger_close_time": [t],
            "base_account": ["W1"],
            "counter_account": ["W2"],
            "base_amount": [100.0],
        }
    )
    df_b = pd.DataFrame(
        {
            "ledger_close_time": [t + pd.Timedelta(minutes=3)],  # within 10-min window
            "base_account": ["W1"],
            "counter_account": ["W3"],
            "base_amount": [80.0],
        }
    )
    trades = {"XLM/USDC": df_a, "XLM/AQUA": df_b}
    corr = [("XLM/USDC", "XLM/AQUA", 0.9)]

    result = find_cross_pair_wallets(trades, corr, time_window_minutes=10)

    assert "W1" in result
    assert "XLM/USDC" in result["W1"]
    assert "XLM/AQUA" in result["W1"]


def test_find_cross_pair_wallets_ignores_non_overlapping_times():
    base = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)

    df_a = pd.DataFrame(
        {
            "ledger_close_time": [base],
            "base_account": ["W1"],
            "counter_account": ["W2"],
            "base_amount": [100.0],
        }
    )
    df_b = pd.DataFrame(
        {
            "ledger_close_time": [base + pd.Timedelta(hours=2)],  # outside 10-min window
            "base_account": ["W1"],
            "counter_account": ["W3"],
            "base_amount": [80.0],
        }
    )
    trades = {"XLM/USDC": df_a, "XLM/AQUA": df_b}
    corr = [("XLM/USDC", "XLM/AQUA", 0.9)]

    result = find_cross_pair_wallets(trades, corr, time_window_minutes=10)

    assert "W1" not in result


def test_find_cross_pair_wallets_empty_correlated_pairs():
    result = find_cross_pair_wallets({}, [], time_window_minutes=10)
    assert result == {}
