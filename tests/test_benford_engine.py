"""Tests for multivariate (cross-pair) Benford analysis.

The univariate Benford tests live in `tests/test_benford.py`; this module covers
the joint digit-distribution / copula coordination detector and its
integrations.
"""

import time

import numpy as np
import pandas as pd

from detection.benford_engine import (
    benford_copula_statistic,
    cross_pair_sync_score,
    digit_entropy_delta,
    joint_digit_matrix,
    multivariate_benford_score,
)
from detection.feature_engineering import (
    MULTIVARIATE_BENFORD_FEATURE_NAMES,
    build_feature_vector,
    multivariate_benford_features,
)
from detection.risk_score import RiskScore

BASE = pd.Timestamp("2026-06-01T00:00:00Z")
COORD_PAIRS = ["XLM/USDC", "XLM/BTC", "USDC/BTC", "XLM/ETH"]


def _asset(code):
    return {"code": code, "issuer": None}


def _trade(wallet, amount, ts, pair):
    base_code, counter_code = pair.split("/")
    return {
        "id": f"{pair}-{ts.isoformat()}-{amount}",
        "ledger_close_time": ts,
        "base_account": wallet,
        "counter_account": "CP",
        "base_asset": _asset(base_code),
        "counter_asset": _asset(counter_code),
        "base_amount": float(amount),
        "counter_amount": float(amount),
        "price": 1.0,
        "base_is_seller": False,
        "trade_type": "orderbook",
        "asset_pair": pair,
    }


def _coordinated_trades(wallet="W"):
    """4 pairs whose amounts are all concentrated on leading digit 1/2 — each
    pair stays plausibly close to Benford alone, but the joint pattern is
    identical across pairs (coordinated manipulation)."""
    amounts = [100.0] * 120 + [150.0] * 40 + [200.0] * 20
    rows = []
    for pair in COORD_PAIRS:
        for i, amt in enumerate(amounts):
            rows.append(_trade(wallet, amt, BASE + pd.Timedelta(seconds=i), pair))
    return pd.DataFrame(rows)


def _iid_benford_trades(wallet="W", n=2000, seed=0):
    """4 pairs of independent Benford-distributed amounts (log-uniform)."""
    rng = np.random.default_rng(seed)
    rows = []
    for pair in COORD_PAIRS:
        amounts = 10 ** rng.uniform(0, 4, n)
        for i, amt in enumerate(amounts):
            rows.append(_trade(wallet, amt, BASE + pd.Timedelta(seconds=int(i)), pair))
    return pd.DataFrame(rows)


# --- copula statistic -------------------------------------------------------


def test_copula_statistic_flags_coordinated_pairs():
    matrix = joint_digit_matrix(_coordinated_trades(), COORD_PAIRS)
    statistic, pval = benford_copula_statistic(matrix)
    assert pval < 0.01
    assert statistic > 0.0


def test_copula_statistic_passes_iid_benford():
    matrix = joint_digit_matrix(_iid_benford_trades(), COORD_PAIRS)
    _, pval = benford_copula_statistic(matrix)
    assert pval > 0.10


def test_copula_statistic_degenerate_single_pair():
    assert benford_copula_statistic(np.zeros((1, 9))) == (0.0, 1.0)


def test_copula_statistic_zeros_matrix():
    stat, pval = benford_copula_statistic(np.zeros((4, 9)))
    assert stat == 0.0
    assert pval == 1.0


def test_joint_digit_matrix_shape_and_rows():
    matrix = joint_digit_matrix(_coordinated_trades(), COORD_PAIRS)
    assert matrix.shape == (len(COORD_PAIRS), 9)
    # each populated row sums to ~1 (a frequency vector)
    assert np.allclose(matrix.sum(axis=1), 1.0)


def test_joint_digit_matrix_none_or_empty():
    matrix = joint_digit_matrix(None, COORD_PAIRS)
    assert matrix.shape == (len(COORD_PAIRS), 9)
    assert np.all(matrix == 0.0)

    empty_df = pd.DataFrame()
    matrix = joint_digit_matrix(empty_df, COORD_PAIRS)
    assert np.all(matrix == 0.0)


# --- synchrony + entropy ----------------------------------------------------


def test_cross_pair_sync_score_high_for_coordinated():
    sync = cross_pair_sync_score(_coordinated_trades(), COORD_PAIRS)
    assert sync > 0.5


def test_cross_pair_sync_score_low_for_iid():
    sync = cross_pair_sync_score(_iid_benford_trades(n=400), COORD_PAIRS)
    assert sync < 0.5


def test_cross_pair_sync_score_empty_or_no_timestamp():
    assert cross_pair_sync_score(None, COORD_PAIRS) == 0.0
    assert cross_pair_sync_score(pd.DataFrame(), COORD_PAIRS) == 0.0
    df = pd.DataFrame({"base_amount": [100.0]})
    assert cross_pair_sync_score(df, COORD_PAIRS) == 0.0


def test_digit_entropy_delta_negative_for_concentrated():
    matrix = joint_digit_matrix(_coordinated_trades(), COORD_PAIRS)
    # concentrated joint distribution is lower-entropy than Benford
    assert digit_entropy_delta(matrix) < 0.0


def test_digit_entropy_delta_empty_or_zero():
    assert digit_entropy_delta(np.zeros((0, 9))) == 0.0
    assert digit_entropy_delta(np.array([]).reshape(0, 0)) == 0.0


# --- multivariate entry point ----------------------------------------------


def test_multivariate_benford_score_coordinated():
    wallet_pairs = [("W", p) for p in COORD_PAIRS]
    result = multivariate_benford_score(_coordinated_trades(), wallet_pairs)
    assert result["copula_pval"] < 0.01
    assert result["sync_ratio"] > 0.5
    assert set(result["pairs"]) == set(COORD_PAIRS)


def test_multivariate_benford_score_iid():
    wallet_pairs = [("W", p) for p in COORD_PAIRS]
    result = multivariate_benford_score(_iid_benford_trades(), wallet_pairs)
    assert result["copula_pval"] > 0.10


def test_multivariate_benford_score_empty_or_single_pair():
    result = multivariate_benford_score(None, [])
    assert result["copula_pval"] == 1.0
    assert result["sync_ratio"] == 0.0

    wallet_pairs = [("W", "XLM/USDC")]
    result = multivariate_benford_score(_coordinated_trades(), wallet_pairs)
    assert result["copula_pval"] == 1.0


# --- feature-engineering integration ---------------------------------------


def test_multivariate_features_present_and_signed():
    trades_by_pair = {p: df for p, df in _coordinated_trades().groupby("asset_pair")}
    feats = multivariate_benford_features("W", trades_by_pair)
    assert set(feats) == set(MULTIVARIATE_BENFORD_FEATURE_NAMES)
    assert feats["benford_copula_pval"] < 0.01
    assert feats["cross_pair_sync_ratio"] > 0.5


def test_multivariate_features_default_without_data():
    feats = multivariate_benford_features("W", None)
    assert feats == {
        "benford_copula_pval": 1.0,
        "cross_pair_sync_ratio": 0.0,
        "digit_entropy_delta": 0.0,
    }


def test_features_flow_through_build_feature_vector():
    trades = _coordinated_trades()
    trades_by_pair = {p: df for p, df in trades.groupby("asset_pair")}
    as_of = trades["ledger_close_time"].max()

    feats = build_feature_vector(trades, "W", as_of, trades_by_pair=trades_by_pair)
    for name in MULTIVARIATE_BENFORD_FEATURE_NAMES:
        assert name in feats
    assert feats["benford_copula_pval"] < 0.01


# --- risk-score integration -------------------------------------------------


def test_copula_pval_flows_into_risk_score():
    baseline = RiskScore.combine(
        wallet="W",
        asset_pair="XLM/USDC",
        benford_mad=0.0,
        benford_mad_threshold=0.015,
        ml_probability=0.0,
        ml_confidence=0.5,
    )
    assert baseline.score == 0

    coordinated = RiskScore.combine(
        wallet="W",
        asset_pair="XLM/USDC",
        benford_mad=0.0,
        benford_mad_threshold=0.015,
        ml_probability=0.0,
        ml_confidence=0.5,
        benford_copula_pval=0.001,
        benford_copula_weight=0.5,
    )
    assert coordinated.score > baseline.score


# --- performance ------------------------------------------------------------


def test_multivariate_performance_10_pairs_24h_under_2s():
    pairs = [f"A{i}/USDC" for i in range(10)]
    rng = np.random.default_rng(3)
    rows = []
    for pair in pairs:
        amounts = 10 ** rng.uniform(0, 4, 500)
        for i, amt in enumerate(amounts):
            ts = BASE + pd.Timedelta(seconds=int(i * (86400 / 500)))
            rows.append(_trade("W", amt, ts, pair))
    trades = pd.DataFrame(rows)
    wallet_pairs = [("W", p) for p in pairs]

    start = time.perf_counter()
    multivariate_benford_score(trades, wallet_pairs)
    assert time.perf_counter() - start < 2.0
