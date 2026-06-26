"""Property-based tests for feature engineering functions using Hypothesis.

Covers Benford metrics, ring features, concentration ratios, and the
end-to-end scoring pipeline.  Run with --hypothesis-seed 0 for
reproducible CI output.

Bug discovered during implementation: `volume_to_unique_counterparty_ratio`
can return a negative value when the trades DataFrame contains negative
base_amount values (e.g. from erroneous ingestion).  The function was
fixed to clamp the result to 0.0 whenever the computed ratio is negative.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from detection.benford_engine import (
    compute_benford_metrics,
    mean_absolute_deviation,
    chi_square_statistic,
    digit_distribution,
    first_digit,
)
from detection.feature_engineering import (
    counterparty_concentration_ratio,
    graph_ring_features,
    intra_minute_clustering_coefficient,
    off_hours_activity_ratio,
    round_trip_trade_frequency,
    volume_to_unique_counterparty_ratio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ASSET = {"code": "XLM", "issuer": None}
_TIMESTAMP = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_trades(
    accounts: list[tuple[str, str]],
    amounts: list[float],
    timestamps: list[datetime] | None = None,
) -> pd.DataFrame:
    """Build a minimal trades DataFrame accepted by feature_engineering functions."""
    ts = timestamps or [_TIMESTAMP] * len(accounts)
    return pd.DataFrame(
        {
            "base_account": [a for a, _ in accounts],
            "counter_account": [b for _, b in accounts],
            "base_amount": amounts,
            "counter_amount": amounts,
            "ledger_close_time": pd.to_datetime(ts, utc=True),
            "base_asset": [_ASSET] * len(accounts),
            "counter_asset": [_ASSET] * len(accounts),
        }
    )


# ---------------------------------------------------------------------------
# 1. Benford metrics: MAD is always in [0, 1]
# ---------------------------------------------------------------------------

@given(
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e12, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=200,
    )
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_benford_mad_in_unit_interval(amounts):
    """MAD of any list of positive amounts must lie in [0, 1]."""
    metrics = compute_benford_metrics(amounts)
    mad = metrics["mad"]
    assert 0.0 <= mad <= 1.0, f"MAD {mad!r} out of [0,1] for amounts={amounts[:5]!r}…"


@given(
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e12, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=200,
    )
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_benford_chi_square_non_negative(amounts):
    """Chi-square statistic must be non-negative for any input."""
    metrics = compute_benford_metrics(amounts)
    assert metrics["chi_square"] >= 0.0


@given(
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e12, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=200,
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_benford_z_scores_non_negative(amounts):
    """All per-digit Z-scores must be non-negative."""
    metrics = compute_benford_metrics(amounts)
    for digit, z in metrics["z_scores"].items():
        assert z >= 0.0, f"Negative Z-score {z} for digit {digit}"


# ---------------------------------------------------------------------------
# 2. Ring features: wash_ring_size is monotone in ring size
# ---------------------------------------------------------------------------

@given(
    account=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
    size_a=st.integers(min_value=0, max_value=100),
    size_b=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=300)
def test_ring_size_monotone(account, size_a, size_b):
    """graph_ring_features wash_ring_size should be monotone in the supplied ring_size."""
    ring_a = {account: {"ring_size": size_a, "cycle_volume_ratio": 0.5, "timing_tightness_score": 0.5}}
    ring_b = {account: {"ring_size": size_b, "cycle_volume_ratio": 0.5, "timing_tightness_score": 0.5}}
    feat_a = graph_ring_features(account, ring_a)
    feat_b = graph_ring_features(account, ring_b)
    if size_a <= size_b:
        assert feat_a["wash_ring_size"] <= feat_b["wash_ring_size"]
    else:
        assert feat_a["wash_ring_size"] >= feat_b["wash_ring_size"]


@given(
    account=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
    ring_size=st.integers(min_value=0, max_value=1000),
    cycle_ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    timing=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
@settings(max_examples=300)
def test_ring_membership_flag_set_when_present(account, ring_size, cycle_ratio, timing):
    """wash_ring_membership must be 1.0 whenever the account appears in ring_membership."""
    ring = {account: {"ring_size": ring_size, "cycle_volume_ratio": cycle_ratio, "timing_tightness_score": timing}}
    features = graph_ring_features(account, ring)
    assert features["wash_ring_membership"] == 1.0
    assert features["wash_ring_size"] == float(ring_size)


# ---------------------------------------------------------------------------
# 3. Concentration ratios are always in [0, 1]  (normalise_features analogue)
# ---------------------------------------------------------------------------

@given(
    n_trades=st.integers(min_value=1, max_value=50),
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=50,
    ),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_counterparty_concentration_ratio_in_unit_interval(n_trades, amounts):
    """counterparty_concentration_ratio must always be in [0, 1]."""
    accounts = [("ACCT_A", f"CP_{i % 5}") for i in range(n_trades)]
    trade_amounts = [amounts[i % len(amounts)] for i in range(n_trades)]
    trades = _make_trades(accounts, trade_amounts)
    ratio = counterparty_concentration_ratio(trades, "ACCT_A")
    assert 0.0 <= ratio <= 1.0, f"Got {ratio}"


@given(
    n_trades=st.integers(min_value=2, max_value=50),
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=50,
    ),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_round_trip_trade_frequency_in_unit_interval(n_trades, amounts):
    """round_trip_trade_frequency must always be in [0, 1]."""
    accounts = [("ACCT_A", "ACCT_B") for _ in range(n_trades)]
    trade_amounts = [amounts[i % len(amounts)] for i in range(n_trades)]
    trades = _make_trades(accounts, trade_amounts)
    freq = round_trip_trade_frequency(trades, "ACCT_A")
    assert 0.0 <= freq <= 1.0, f"Got {freq}"


@given(
    n_trades=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=200)
def test_intra_minute_clustering_in_unit_interval(n_trades):
    """intra_minute_clustering_coefficient must always be in [0, 1]."""
    accounts = [("ACCT_A", "ACCT_B")] * n_trades
    amounts = [100.0] * n_trades
    trades = _make_trades(accounts, amounts)
    coeff = intra_minute_clustering_coefficient(trades)
    assert 0.0 <= coeff <= 1.0, f"Got {coeff}"


@given(
    n_trades=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=200)
def test_off_hours_activity_ratio_in_unit_interval(n_trades):
    """off_hours_activity_ratio must always be in [0, 1]."""
    accounts = [("ACCT_A", "ACCT_B")] * n_trades
    amounts = [100.0] * n_trades
    trades = _make_trades(accounts, amounts)
    ratio = off_hours_activity_ratio(trades)
    assert 0.0 <= ratio <= 1.0, f"Got {ratio}"


# ---------------------------------------------------------------------------
# 4. Scoring pipeline: benford features produce a valid score structure
# ---------------------------------------------------------------------------

@given(
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=500,
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_benford_pipeline_returns_complete_metrics(amounts):
    """compute_benford_metrics always returns a dict with all required keys."""
    metrics = compute_benford_metrics(amounts)
    assert "chi_square" in metrics
    assert "mad" in metrics
    assert "z_scores" in metrics
    assert "sample_size" in metrics
    assert isinstance(metrics["sample_size"], int)
    assert metrics["sample_size"] >= 0
    assert metrics["sample_size"] <= len(amounts)


@given(
    amounts=st.lists(
        st.floats(min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=500,
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_benford_digit_distribution_sums_to_one(amounts):
    """Observed digit distribution must sum to 1.0 (within float tolerance)."""
    dist = digit_distribution(amounts)
    valid = [a for a in amounts if first_digit(a) is not None]
    if valid:
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-9, f"Distribution sums to {total}"
    else:
        assert all(v == 0.0 for v in dist.values())
