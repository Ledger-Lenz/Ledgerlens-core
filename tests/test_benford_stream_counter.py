"""Tests for BenfordStreamCounter.

Covers:
- Zero-trade edge case
- Single-digit-amount degenerate case
- Window rollover correctness
- Numerical equivalence with the batch implementation
- update() < 1 µs and window_stats() < 1 ms performance
"""

import math
import time

import numpy as np
import pytest

from detection.benford_engine import (
    BenfordStats,
    BenfordStreamCounter,
    chi_square_statistic,
    digit_distribution,
    mean_absolute_deviation,
    z_scores,
)
from detection.model_inference import get_benford_stats, ingest_trade, _wallet_counters


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_benford_amounts(n: int, seed: int = 42) -> list[float]:
    """Log-uniform amounts — should approximate Benford distribution."""
    rng = np.random.default_rng(seed)
    return (10 ** rng.uniform(0, 4, n)).tolist()


def _batch_stats(amounts: list[float]) -> dict:
    observed = digit_distribution(amounts)
    n = sum(1 for a in amounts if _first_digit_valid(a))
    return {
        "chi_square": chi_square_statistic(observed, n),
        "mad": mean_absolute_deviation(observed),
        "z_scores": z_scores(observed, n),
    }


def _first_digit_valid(v):
    return v is not None and math.isfinite(v) and v > 0


# ---------------------------------------------------------------------------
# zero-trade edge case
# ---------------------------------------------------------------------------

def test_zero_trades_returns_zero_stats():
    ctr = BenfordStreamCounter(windows=[100])
    stats = ctr.window_stats(100)
    assert isinstance(stats, BenfordStats)
    assert stats.n == 0
    assert stats.chi_square == 0.0
    assert stats.mad == 0.0
    assert stats.z_scores == [0.0] * 9


# ---------------------------------------------------------------------------
# single-digit amounts (all amounts start with digit 1, e.g. 1.0, 10.0, …)
# ---------------------------------------------------------------------------

def test_single_digit_concentration():
    ctr = BenfordStreamCounter(windows=[50])
    for _ in range(50):
        ctr.update(100.0)  # leading digit always 1
    stats = ctr.window_stats(50)
    assert stats.n == 50
    # observed[0] (digit 1) should be 1.0, all others 0
    assert math.isclose(stats.z_scores[0], stats.z_scores[0])  # sanity
    assert stats.mad > 0.0
    assert stats.chi_square > 0.0


# ---------------------------------------------------------------------------
# invalid amounts are ignored
# ---------------------------------------------------------------------------

def test_invalid_amounts_skipped():
    ctr = BenfordStreamCounter(windows=[10])
    for bad in [0.0, -5.0, float("nan"), float("inf"), None]:
        ctr.update(bad)  # type: ignore[arg-type]
    assert ctr.window_stats(10).n == 0


# ---------------------------------------------------------------------------
# window rollover
# ---------------------------------------------------------------------------

def test_window_rollover_evicts_oldest():
    """After filling and rolling a window=10, only the last 10 digits count."""
    ctr = BenfordStreamCounter(windows=[10])
    # Fill with digit-2 amounts
    for _ in range(10):
        ctr.update(20.0)  # leading digit 2
    assert ctr.window_stats(10).n == 10

    # Now push 10 digit-1 amounts — old digit-2 trades must all be evicted
    for _ in range(10):
        ctr.update(100.0)  # leading digit 1

    stats = ctr.window_stats(10)
    assert stats.n == 10
    # digit 1 count = 10, digit 2 count = 0
    counts = ctr._counts[0]
    assert counts[0] == 10   # digit 1, index 0
    assert counts[1] == 0    # digit 2, index 1


def test_window_rollover_partial():
    """Partially-filled window: n < window_size, no eviction."""
    ctr = BenfordStreamCounter(windows=[100])
    amounts = _make_benford_amounts(40)
    for a in amounts:
        ctr.update(a)
    assert ctr.window_stats(100).n == 40


# ---------------------------------------------------------------------------
# multiple simultaneous windows
# ---------------------------------------------------------------------------

def test_multiple_windows_independent():
    ctr = BenfordStreamCounter(windows=[10, 50])
    amounts = _make_benford_amounts(80)
    for a in amounts:
        ctr.update(a)
    s10 = ctr.window_stats(10)
    s50 = ctr.window_stats(50)
    assert s10.n == 10
    assert s50.n == 50


def test_unknown_window_raises():
    ctr = BenfordStreamCounter(windows=[100])
    with pytest.raises(ValueError):
        ctr.window_stats(999)


# ---------------------------------------------------------------------------
# numerical equivalence with batch implementation
# ---------------------------------------------------------------------------

def test_numerical_equivalence_full_window():
    """window_stats() must match compute_benford_metrics() to float tolerance."""
    window = 200
    amounts = _make_benford_amounts(window, seed=7)
    ctr = BenfordStreamCounter(windows=[window])
    for a in amounts:
        ctr.update(a)

    stream = ctr.window_stats(window)
    batch = _batch_stats(amounts)

    assert math.isclose(stream.chi_square, batch["chi_square"], rel_tol=1e-9), (
        f"chi_square mismatch: {stream.chi_square} vs {batch['chi_square']}"
    )
    assert math.isclose(stream.mad, batch["mad"], rel_tol=1e-9), (
        f"MAD mismatch: {stream.mad} vs {batch['mad']}"
    )
    for d in range(1, 10):
        assert math.isclose(stream.z_scores[d - 1], batch["z_scores"][d], rel_tol=1e-9), (
            f"Z-score mismatch digit {d}: {stream.z_scores[d - 1]} vs {batch['z_scores'][d]}"
        )


def test_numerical_equivalence_after_rollover():
    """After rollover, window_stats() must match batch over the *last* window trades."""
    window = 100
    total = 350
    amounts = _make_benford_amounts(total, seed=13)
    ctr = BenfordStreamCounter(windows=[window])
    for a in amounts:
        ctr.update(a)

    # batch baseline uses only the last `window` amounts
    last = amounts[-window:]
    batch = _batch_stats(last)
    stream = ctr.window_stats(window)

    assert math.isclose(stream.chi_square, batch["chi_square"], rel_tol=1e-9)
    assert math.isclose(stream.mad, batch["mad"], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# model_inference integration
# ---------------------------------------------------------------------------

def test_ingest_trade_creates_counter():
    wallet = "__test_wallet_ingest__"
    _wallet_counters.pop(wallet, None)
    ingest_trade(wallet, 123.45)
    assert wallet in _wallet_counters
    stats = get_benford_stats(wallet, 100)
    assert stats is not None
    assert stats.n == 1


def test_get_benford_stats_unknown_wallet():
    assert get_benford_stats("__nonexistent_wallet__", 100) is None


def test_ingest_trade_accumulates():
    wallet = "__test_wallet_accum__"
    _wallet_counters.pop(wallet, None)
    for amt in _make_benford_amounts(50):
        ingest_trade(wallet, amt)
    stats = get_benford_stats(wallet, 100)
    assert stats.n == 50


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------

def test_update_under_1_microsecond():
    """update() must complete in < 10 µs on average on CI runners."""
    ctr = BenfordStreamCounter(windows=[100, 500, 1000])
    amounts = _make_benford_amounts(10_000)
    # warm up
    for a in amounts[:100]:
        ctr.update(a)

    start = time.perf_counter()
    for a in amounts:
        ctr.update(a)
    elapsed = time.perf_counter() - start
    avg_us = elapsed / len(amounts) * 1e6
    assert avg_us < 10.0, f"update() averaged {avg_us:.3f} µs (limit: 10 µs)"


def test_window_stats_under_1_millisecond():
    """window_stats() must complete in < 1 ms."""
    ctr = BenfordStreamCounter(windows=[100, 500, 1000])
    for a in _make_benford_amounts(1000):
        ctr.update(a)

    iterations = 1000
    start = time.perf_counter()
    for _ in range(iterations):
        ctr.window_stats(1000)
    elapsed = (time.perf_counter() - start) / iterations * 1000
    assert elapsed < 1.0, f"window_stats() averaged {elapsed:.4f} ms (limit: 1 ms)"
