"""Tests for the Monte Carlo bootstrap p-value extension to the Benford engine.

Covers: calibration, power, method-selection boundary, floor p-value,
LRU cache, vectorised performance, N=0 edge case, BenfordWindowFeatures
field population, and integration with the full pipeline for small windows.
"""

import time
from unittest.mock import patch

import numpy as np

from detection.benford_engine import (
    BENFORD_PROBS,
    BenfordWindowFeatures,
    _cached_bootstrap_pvalue,
    bootstrap_chi_square_pvalue,
    compute_benford_metrics,
    compute_chi_square_pvalue,
)


# --------------------------------------------------------------------------- #
# Calibration — false-positive rate under the null should be ≈ 0.05
# --------------------------------------------------------------------------- #

def test_calibration_false_positive_rate():
    """Under true Benford null, ~5% of p-values fall below 0.05.

    Generates 1,000 samples of N=30 from the Benford distribution and checks
    that the empirical Type-I error rate is within [0.03, 0.07].
    """
    rng = np.random.default_rng(2024)
    n_trials = 1_000
    n_per_trial = 30
    below = sum(
        1
        for _ in range(n_trials)
        if bootstrap_chi_square_pvalue(
            rng.multinomial(n_per_trial, BENFORD_PROBS),
            n_bootstrap=10_000,
            seed=None,
        )
        < 0.05
    )
    fpr = below / n_trials
    assert 0.03 <= fpr <= 0.07, f"False-positive rate {fpr:.3f} outside [0.03, 0.07]"


# --------------------------------------------------------------------------- #
# Power — uniform (non-Benford) distribution should be reliably rejected
# --------------------------------------------------------------------------- #

def test_power_uniform_distribution():
    """Bootstrap p-values for a uniform digit distribution should be < 0.01 on average.

    Uniform leading-digit distribution is maximally non-Benford; the test
    should reject it with overwhelming probability at any reasonable N.
    """
    rng = np.random.default_rng(7)
    uniform_probs = np.ones(9) / 9
    p_values = [
        bootstrap_chi_square_pvalue(
            rng.multinomial(50, uniform_probs),
            n_bootstrap=10_000,
            seed=None,
        )
        for _ in range(100)
    ]
    assert np.mean(p_values) < 0.01, f"Mean p-value {np.mean(p_values):.4f} >= 0.01 for uniform distribution"


# --------------------------------------------------------------------------- #
# Method selection at the N = BENFORD_BOOTSTRAP_THRESHOLD boundary
# --------------------------------------------------------------------------- #

def test_method_selection_boundary():
    """N=99 → bootstrap; N=100 → asymptotic; N=101 → asymptotic."""
    rng = np.random.default_rng(0)

    for n, expected_method in [(99, "bootstrap"), (100, "asymptotic"), (101, "asymptotic")]:
        counts = rng.multinomial(n, BENFORD_PROBS)
        _, method = compute_chi_square_pvalue(counts, n)
        assert method == expected_method, f"N={n}: expected '{expected_method}', got '{method}'"


# --------------------------------------------------------------------------- #
# Floor p-value — near-perfect Benford counts should yield a high p-value
# --------------------------------------------------------------------------- #

def test_floor_pvalue_near_benford():
    """Counts that closely match the Benford distribution should return p > 0.5."""
    n = 50
    counts = np.round(BENFORD_PROBS * n).astype(int)
    # Correct rounding drift so counts sum exactly to n
    diff = n - int(counts.sum())
    counts[0] += diff

    p = bootstrap_chi_square_pvalue(counts, n_bootstrap=10_000, seed=42)
    assert p > 0.5, f"Expected p > 0.5 for near-Benford counts, got {p:.4f}"


# --------------------------------------------------------------------------- #
# LRU cache — same inputs must not trigger a second computation
# --------------------------------------------------------------------------- #

def test_lru_cache_hit():
    """Second call with identical arguments must not call bootstrap_chi_square_pvalue again."""
    counts_tuple = (30, 18, 12, 10, 8, 7, 6, 5, 4)
    _cached_bootstrap_pvalue.cache_clear()

    with patch(
        "detection.benford_engine.bootstrap_chi_square_pvalue",
        return_value=0.42,
    ) as mock_fn:
        result1 = _cached_bootstrap_pvalue(counts_tuple, 1_000, 99)
        result2 = _cached_bootstrap_pvalue(counts_tuple, 1_000, 99)

    mock_fn.assert_called_once()
    assert result1 == result2 == 0.42


# --------------------------------------------------------------------------- #
# Vectorised performance — must complete in < 500 ms for N=50, 10 k samples
# --------------------------------------------------------------------------- #

def test_vectorised_performance():
    """bootstrap_chi_square_pvalue with N=50, n_bootstrap=10_000 must finish in < 500 ms."""
    rng = np.random.default_rng(13)
    counts = rng.multinomial(50, BENFORD_PROBS)

    start = time.perf_counter()
    bootstrap_chi_square_pvalue(counts, n_bootstrap=10_000, seed=None)
    elapsed_ms = (time.perf_counter() - start) * 1_000

    assert elapsed_ms < 500, f"Bootstrap took {elapsed_ms:.0f} ms; limit is 500 ms"


# --------------------------------------------------------------------------- #
# N = 0 edge case
# --------------------------------------------------------------------------- #

def test_empty_counts_returns_one():
    """Zero counts → p_value = 1.0 with method 'bootstrap' (can't reject null)."""
    counts = np.zeros(9, dtype=int)
    p, method = compute_chi_square_pvalue(counts, 0)
    assert p == 1.0
    assert method == "bootstrap"


# --------------------------------------------------------------------------- #
# BenfordWindowFeatures dataclass — field population for both methods
# --------------------------------------------------------------------------- #

def test_benford_window_features_bootstrap_method():
    """BenfordWindowFeatures.chi_square_pvalue_method holds 'bootstrap' for N < threshold."""
    features = BenfordWindowFeatures(
        window_hours=1,
        n_transactions=25,
        chi_square_stat=5.0,
        chi_square_pvalue=0.3,
        chi_square_pvalue_method="bootstrap",
        mad=0.01,
        z_scores=[0.1] * 9,
        benford_flag=False,
    )
    assert features.chi_square_pvalue_method == "bootstrap"
    assert features.n_transactions == 25


def test_benford_window_features_asymptotic_method():
    """BenfordWindowFeatures.chi_square_pvalue_method holds 'asymptotic' for N >= threshold."""
    features = BenfordWindowFeatures(
        window_hours=24,
        n_transactions=150,
        chi_square_stat=3.0,
        chi_square_pvalue=0.8,
        chi_square_pvalue_method="asymptotic",
        mad=0.008,
        z_scores=[0.05] * 9,
        benford_flag=False,
    )
    assert features.chi_square_pvalue_method == "asymptotic"
    assert features.n_transactions == 150


def test_compute_benford_metrics_populates_pvalue_method_small():
    """compute_benford_metrics returns pvalue_method='bootstrap' for N < threshold."""
    # 25 amounts — all small windows (1h / 4h) typically have N < 100 on SDEX
    amounts = [100.0, 250.0, 31.5, 47.8, 55.0] * 5
    metrics = compute_benford_metrics(amounts)

    assert "chi_square_pvalue" in metrics
    assert "pvalue_method" in metrics
    assert metrics["pvalue_method"] == "bootstrap"
    assert 0.0 < metrics["chi_square_pvalue"] <= 1.0


def test_compute_benford_metrics_populates_pvalue_method_large():
    """compute_benford_metrics returns pvalue_method='asymptotic' for N >= threshold."""
    rng = np.random.default_rng(55)
    # Generate 120 amounts with Benford-like leading digits
    amounts = []
    for d, p in enumerate(BENFORD_PROBS, start=1):
        n_d = max(1, round(p * 120))
        amounts.extend([float(d) * (1 + rng.random() * 0.5) for _ in range(n_d)])

    metrics = compute_benford_metrics(amounts[:120])
    assert metrics["pvalue_method"] == "asymptotic"
    assert 0.0 <= metrics["chi_square_pvalue"] <= 1.0


# --------------------------------------------------------------------------- #
# Integration — small 1h window wallet uses bootstrap
# --------------------------------------------------------------------------- #

def test_integration_small_window_bootstrap():
    """Wallet with 25 trades in the 1h window produces pvalue_method='bootstrap'."""
    amounts = [127.5, 43.2, 89.1, 215.0, 67.8] * 5  # 25 trades
    metrics = compute_benford_metrics(amounts)

    assert metrics["sample_size"] == 25
    assert metrics["pvalue_method"] == "bootstrap"
    assert 0.0 < metrics["chi_square_pvalue"] <= 1.0
    # Existing keys are still present (backward compatibility)
    assert "chi_square" in metrics
    assert "mad" in metrics
    assert "z_scores" in metrics
