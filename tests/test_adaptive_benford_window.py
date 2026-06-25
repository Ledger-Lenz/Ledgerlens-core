"""Tests for AdaptiveBenfordWindow (Issue #102).

Verifies that windows are expanded when sample count is below the minimum,
that the valid/expanded/merged flags are set correctly, and that the
benford_window_expanded_{label} feature flags flow through
build_feature_vector.
"""

import pandas as pd
import pytest

from detection.benford_engine import AdaptiveBenfordWindow, BenfordWindowResult
from detection.feature_engineering import (
    BENFORD_WINDOW_EXPANDED_FEATURE_NAMES,
    ROLLING_WINDOWS,
    benford_features,
    build_feature_vector,
)

BASE = pd.Timestamp("2026-06-01T00:00:00Z")


def _make_trades(n: int, window_label: str = "1h", start: pd.Timestamp = BASE) -> pd.DataFrame:
    """Return `n` trades uniformly spread over the specified window."""
    window = ROLLING_WINDOWS[window_label]
    step = window / max(n, 1)
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": str(i),
                "ledger_close_time": start - window + (i + 1) * step,
                "base_account": "W",
                "counter_account": "CP",
                "base_asset": {"code": "XLM", "issuer": None},
                "counter_asset": {"code": "USDC", "issuer": None},
                "base_amount": float(100 + i),
                "counter_amount": float(100 + i),
                "price": 1.0,
                "base_is_seller": False,
                "trade_type": "orderbook",
                "liquidity_pool_id": None,
            }
        )
    return pd.DataFrame(rows)


# ---- BenfordWindowResult flags ----------------------------------------


def test_fit_returns_valid_when_sufficient_trades():
    trades = _make_trades(50, "1h")
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    result = aw.fit(trades, "1h", BASE, ROLLING_WINDOWS)
    assert isinstance(result, BenfordWindowResult)
    assert result.valid is True
    assert result.expanded is False
    assert result.merged is False
    assert len(result.trades) >= 30


def test_fit_expands_when_too_few_trades():
    # Only 5 trades in the 1h window; need 30 so it should expand.
    trades = _make_trades(5, "1h")
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    result = aw.fit(trades, "1h", BASE, ROLLING_WINDOWS)
    # Expanded or merged (all 5 trades used), but valid=False (still < 30).
    assert result.expanded is True or result.merged is True


def test_fit_merged_when_no_window_reaches_min():
    # 3 trades total, min=30 → no expansion can help; should merge.
    trades = _make_trades(3, "30d")
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    result = aw.fit(trades, "1h", BASE, ROLLING_WINDOWS)
    assert result.merged is True
    assert result.valid is False  # still < 30 trades


def test_fit_marks_effective_width_as_at_most_max_window():
    trades = _make_trades(5, "1h")
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=7)
    result = aw.fit(trades, "1h", BASE, ROLLING_WINDOWS)
    assert result.effective_width <= pd.Timedelta(days=7)


# ---- benford_features integration ----------------------------------------


def test_benford_features_returns_expanded_flags():
    trades = _make_trades(5, "1h")  # too few → expanded
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    feats = benford_features(trades, BASE, adaptive_window=aw)
    assert "benford_window_expanded_1h" in feats
    assert feats["benford_window_expanded_1h"] == 1.0


def test_benford_features_no_expansion_flag_for_sufficient_trades():
    trades = _make_trades(50, "1h")
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    feats = benford_features(trades, BASE, adaptive_window=aw)
    assert feats["benford_window_expanded_1h"] == 0.0


def test_benford_features_without_adaptive_window_sets_zero_flags():
    trades = _make_trades(5, "1h")
    feats = benford_features(trades, BASE, adaptive_window=None)
    for label in ROLLING_WINDOWS:
        assert feats[f"benford_window_expanded_{label}"] == 0.0


# ---- FEATURE_NAMES / build_feature_vector ---------------------------------


def test_expanded_feature_names_in_feature_names_list():
    for name in BENFORD_WINDOW_EXPANDED_FEATURE_NAMES:
        from detection.feature_engineering import FEATURE_NAMES
        assert name in FEATURE_NAMES, f"{name} not in FEATURE_NAMES"


def test_expanded_flags_flow_through_build_feature_vector():
    trades = _make_trades(5, "1h")  # sparse → will expand
    aw = AdaptiveBenfordWindow(min_sample_count=30, max_window_days=90)
    feats = build_feature_vector(
        trades, "W", BASE, adaptive_benford_window=aw
    )
    assert "benford_window_expanded_1h" in feats


def test_all_expanded_flags_present_in_build_feature_vector():
    trades = _make_trades(50, "30d")
    aw = AdaptiveBenfordWindow(min_sample_count=5, max_window_days=90)
    feats = build_feature_vector(
        trades, "W", BASE, adaptive_benford_window=aw
    )
    for name in BENFORD_WINDOW_EXPANDED_FEATURE_NAMES:
        assert name in feats
