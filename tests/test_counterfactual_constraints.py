"""Tests for detection/counterfactual_constraints.py and related counterfactual modules."""

import pytest

from detection.counterfactual_constraints import (
    FEATURE_CONSTRAINTS,
    FeatureConstraint,
    _immutable,
    _decreasable,
    _increasable,
    get_mutable_features,
)


# ---------------------------------------------------------------------------
# FeatureConstraint dataclass
# ---------------------------------------------------------------------------

def test_feature_constraint_is_frozen():
    """FeatureConstraint is immutable (frozen dataclass)."""
    c = _decreasable("test_feature")
    with pytest.raises(Exception):
        c.mutable = False  # type: ignore[misc]


def test_immutable_helper():
    c = _immutable("age_days")
    assert c.feature_name == "age_days"
    assert c.mutable is False
    assert c.direction == "any"
    assert c.min_val is None
    assert c.max_val is None


def test_decreasable_helper():
    c = _decreasable("wash_score")
    assert c.feature_name == "wash_score"
    assert c.mutable is True
    assert c.direction == "decrease"
    assert c.min_val == 0.0
    assert c.max_val is None

    c_custom = _decreasable("sync_score", min_val=-1.0)
    assert c_custom.min_val == -1.0


def test_increasable_helper():
    c = _increasable("pdc_5m", max_val=1.0)
    assert c.feature_name == "pdc_5m"
    assert c.mutable is True
    assert c.direction == "increase"
    assert c.min_val is None
    assert c.max_val == 1.0

    c_unbounded = _increasable("lag_hours")
    assert c_unbounded.max_val is None


# ---------------------------------------------------------------------------
# No duplicate feature names in FEATURE_CONSTRAINTS
# ---------------------------------------------------------------------------

def test_no_duplicate_feature_constraints():
    """FEATURE_CONSTRAINTS must not contain duplicate feature names."""
    names = [c.feature_name for c in FEATURE_CONSTRAINTS]
    duplicates = [n for n in names if names.count(n) > 1]
    assert not duplicates, (
        f"Duplicate feature constraints found: {sorted(set(duplicates))}"
    )


# ---------------------------------------------------------------------------
# get_mutable_features
# ---------------------------------------------------------------------------

def test_get_mutable_features_returns_list_of_strings():
    features = get_mutable_features()
    assert isinstance(features, list)
    assert all(isinstance(f, str) for f in features)
    assert len(features) > 0


def test_get_mutable_features_excludes_immutable():
    features = get_mutable_features()
    assert "account_age_days" not in features
    assert "has_evm_link" not in features


def test_get_mutable_features_includes_decreasable():
    features = get_mutable_features()
    assert "counterparty_concentration_ratio" in features
    assert "self_matching_rate" in features


def test_get_mutable_features_is_idempotent():
    """Calling get_mutable_features twice returns the same list."""
    f1 = get_mutable_features()
    f2 = get_mutable_features()
    assert f1 == f2
    # Also check it's the same object (cached)
    assert f1 is f2


def test_get_mutable_features_consistency():
    """Every constraint marked mutable must appear in get_mutable_features."""
    mutable_from_constraints = {c.feature_name for c in FEATURE_CONSTRAINTS if c.mutable}
    mutable_from_function = set(get_mutable_features())
    assert mutable_from_constraints == mutable_from_function


# ---------------------------------------------------------------------------
# Well-known features have expected directions
# ---------------------------------------------------------------------------

def test_cross_chain_time_lag_is_increasable():
    """cross_chain_time_lag_median_h is the only 'increase' cross-chain feature."""
    by_name = {c.feature_name: c for c in FEATURE_CONSTRAINTS}
    c = by_name["cross_chain_time_lag_median_h"]
    assert c.mutable is True
    assert c.direction == "increase"
    assert c.max_val == 720.0


def test_benford_copula_pval_is_increasable():
    by_name = {c.feature_name: c for c in FEATURE_CONSTRAINTS}
    c = by_name["benford_copula_pval"]
    assert c.mutable is True
    assert c.direction == "increase"
    assert c.max_val == 1.0


def test_amm_tenure_ratio_is_increasable():
    by_name = {c.feature_name: c for c in FEATURE_CONSTRAINTS}
    c = by_name["amm_tenure_ratio"]
    assert c.mutable is True
    assert c.direction == "increase"
    assert c.max_val == 1.0


def test_pdc_features_are_increasable():
    by_name = {c.feature_name: c for c in FEATURE_CONSTRAINTS}
    for name in ("pdc_5m", "pdc_1h"):
        c = by_name[name]
        assert c.mutable is True
        assert c.direction == "increase"
        assert c.max_val == 1.0


def test_immutable_features_are_not_mutable():
    by_name = {c.feature_name: c for c in FEATURE_CONSTRAINTS}
    for name in ("account_age_days", "has_evm_link"):
        assert by_name[name].mutable is False
