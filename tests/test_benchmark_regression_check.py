"""Tests for the dependency-free regression-detection logic in
benchmarks/benchmark_scoring.py (percentiles, baseline comparison, linear
scaling check). Does not exercise the actual pipeline benchmarks
themselves (those need numpy/pandas/scikit-learn) -- only the pure-stdlib
analysis functions that CI uses to pass/fail the benchmark job.
"""

import pytest

from benchmarks.benchmark_scoring import (
    _hardware_fingerprint,
    _percentiles,
    check_linear_scaling,
    check_regressions,
)


def test_percentiles_basic_shape():
    samples = [i / 1000 for i in range(1, 101)]  # 1ms .. 100ms
    result = _percentiles(samples)
    assert result["samples"] == 100
    assert result["p50_ms"] == pytest.approx(50.0, abs=1.0)
    assert result["p99_ms"] == pytest.approx(99.0, abs=1.0)
    assert result["p95_ms"] <= result["p99_ms"]
    assert result["p50_ms"] <= result["p95_ms"]


def test_hardware_fingerprint_has_expected_keys():
    fp = _hardware_fingerprint()
    assert set(fp.keys()) == {"cpu", "ram_gb", "platform", "python_version"}


def test_check_regressions_no_baseline_entry_is_skipped():
    results = {"scenarios": {"new_scenario": {"p99_ms": 100.0}}}
    baseline = {"scenarios": {}}
    assert check_regressions(results, baseline) == []


def test_check_regressions_under_threshold_passes():
    baseline = {"scenarios": {"single_wallet_score": {"p99_ms": 10.0}}}
    results = {"scenarios": {"single_wallet_score": {"p99_ms": 11.5}}}  # +15%
    assert check_regressions(results, baseline) == []


def test_check_regressions_over_threshold_fails():
    baseline = {"scenarios": {"single_wallet_score": {"p99_ms": 10.0}}}
    results = {"scenarios": {"single_wallet_score": {"p99_ms": 13.0}}}  # +30%
    failures = check_regressions(results, baseline)
    assert len(failures) == 1
    assert "single_wallet_score" in failures[0]


def test_check_regressions_catches_artificial_2x_regression():
    """Mirrors the issue's literal acceptance criterion: an artificially
    introduced 2x (100%) regression must be caught."""
    baseline = {
        "scenarios": {
            "single_wallet_score": {"p99_ms": 10.0},
            "feature_extraction_1000_trades": {"p99_ms": 50.0},
            "batch_scoring_100_wallets": {"p99_ms": 200.0},
        }
    }
    results = {
        "scenarios": {
            "single_wallet_score": {"p99_ms": 20.0},  # 2x regression
            "feature_extraction_1000_trades": {"p99_ms": 51.0},  # fine
            "batch_scoring_100_wallets": {"p99_ms": 205.0},  # fine
        }
    }
    failures = check_regressions(results, baseline)
    assert len(failures) == 1
    assert "single_wallet_score" in failures[0]
    assert "100.0%" in failures[0]


def test_check_regressions_improvement_never_fails():
    baseline = {"scenarios": {"single_wallet_score": {"p99_ms": 10.0}}}
    results = {"scenarios": {"single_wallet_score": {"p99_ms": 5.0}}}  # 2x faster
    assert check_regressions(results, baseline) == []


def test_check_linear_scaling_passes_when_batch_is_cheaper_per_wallet():
    results = {
        "scenarios": {
            "single_wallet_score": {"p50_ms": 1.0},
            "batch_scoring_100_wallets": {"per_wallet_p50_ms": 0.5},
        }
    }
    assert check_linear_scaling(results) == []


def test_check_linear_scaling_fails_when_batch_is_much_more_expensive_per_wallet():
    results = {
        "scenarios": {
            "single_wallet_score": {"p50_ms": 1.0},
            "batch_scoring_100_wallets": {"per_wallet_p50_ms": 5.0},
        }
    }
    failures = check_linear_scaling(results)
    assert len(failures) == 1
    assert "batch_scoring_100_wallets" in failures[0]


def test_check_linear_scaling_missing_scenarios_is_noop():
    assert check_linear_scaling({"scenarios": {}}) == []
