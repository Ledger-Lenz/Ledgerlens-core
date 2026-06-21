"""Tests for the causal inference engine (price discovery contribution)."""

import numpy as np
import pandas as pd

from detection.causal_engine import estimate_pdc, propensity_score
from detection.feature_engineering import (
    CAUSAL_FEATURE_NAMES,
    build_feature_vector,
    causal_features,
)
from detection.risk_score import RiskScore
from detection.shap_explainer import annotate_causal_features, pdc_annotation
from ingestion.data_models import TradeType

PAIR = "XLM/USDC"
XLM = {"code": "XLM", "issuer": None}
USDC = {"code": "USDC", "issuer": "GISSUER"}
START = pd.Timestamp("2026-06-01T00:00:00Z")
WINDOW = pd.Timedelta(minutes=5)
N = 30


def _prices(increment_for_window) -> pd.DataFrame:
    """Build a 5-minute mid-price series; `increment_for_window(i)` is Δprice for window i."""
    rows = []
    price = 100.0
    for i in range(N + 1):
        rows.append({"timestamp": START + i * WINDOW, "mid_price": price})
        price += increment_for_window(i)
    return pd.DataFrame(rows)


def _trades(attacker_trades_window) -> pd.DataFrame:
    """Market wallets trade every window; the studied wallet trades when
    `attacker_trades_window(i)` is true.

    Market volume is randomised and large so it does not become a proxy for the
    wallet's (small) treatment — otherwise the confounder would be collinear
    with treatment and the effect would be unidentifiable.
    """
    rng = np.random.default_rng(7)
    rows = []
    for i in range(N):
        ts = START + i * WINDOW + pd.Timedelta(minutes=1)
        rows.append(_trade("M1", "M2", float(rng.uniform(800, 1200)), ts))
        if attacker_trades_window(i):
            rows.append(_trade("W", "M2", 50.0, ts + pd.Timedelta(seconds=30)))
    return pd.DataFrame(rows)


def _trade(base, counter, amount, ts, price=1.0):
    return {
        "id": f"{base}-{ts.isoformat()}",
        "ledger_close_time": ts,
        "base_account": base,
        "counter_account": counter,
        "base_asset": XLM,
        "counter_asset": USDC,
        "base_amount": amount,
        "counter_amount": amount * price,
        "price": price,
        "base_is_seller": False,
        "trade_type": TradeType.ORDERBOOK,
        "liquidity_pool_id": None,
        "asset_pair": PAIR,
    }


def _is_even(i):
    return i % 2 == 0


# --- core PDC estimation ----------------------------------------------------


def test_pdc_positive_for_market_maker():
    # Wallet trades in even windows; price rises after exactly those windows.
    prices = _prices(lambda i: 0.5 if _is_even(i) else 0.0)
    trades = _trades(_is_even)

    pdc = estimate_pdc(trades, prices, "W", PAIR, window_minutes=5)
    assert pdc > 0.2


def test_pdc_near_zero_for_wash_trader():
    # Wallet trades in even windows, but the price path oscillates on a
    # different period (i % 4), so trading does not cause price movement.
    prices = _prices(lambda i: 0.3 if (i % 4) < 2 else -0.3)
    trades = _trades(_is_even)

    pdc = estimate_pdc(trades, prices, "W", PAIR, window_minutes=5)
    assert abs(pdc) < 0.15


def test_pdc_market_maker_exceeds_wash_trader():
    mm = estimate_pdc(_trades(_is_even), _prices(lambda i: 0.5 if _is_even(i) else 0.0), "W", PAIR)
    wt = estimate_pdc(_trades(_is_even), _prices(lambda i: 0.3 if (i % 4) < 2 else -0.3), "W", PAIR)
    assert mm > wt + 0.2


def test_pdc_returns_zero_without_prices():
    trades = _trades(_is_even)
    assert estimate_pdc(trades, pd.DataFrame(), "W", PAIR) == 0.0


def test_pdc_returns_zero_without_treatment_overlap():
    # Wallet never trades -> no treated windows -> effect unidentifiable.
    prices = _prices(lambda i: 0.5 if _is_even(i) else 0.0)
    trades = _trades(lambda i: False)
    assert estimate_pdc(trades, prices, "W", PAIR) == 0.0


# --- propensity scoring -----------------------------------------------------


def test_propensity_score_returns_probabilities():
    rng = np.random.default_rng(0)
    n = 40
    treated = np.array([i % 2 for i in range(n)])
    # confounder correlated with treatment
    x = treated + rng.normal(0, 0.5, n)
    features = pd.DataFrame({"x": x, "volatility": rng.random(n), "treated": treated})

    probs = propensity_score(features)
    assert probs.shape == (n,)
    assert np.all(probs > 0.0) and np.all(probs < 1.0)


def test_propensity_score_single_class_returns_base_rate():
    features = pd.DataFrame({"x": [0.1, 0.2, 0.3], "treated": [1, 1, 1]})
    probs = propensity_score(features)
    assert np.allclose(probs, 1.0)


# --- feature-engineering integration ---------------------------------------


def test_causal_features_present_and_signed():
    prices = _prices(lambda i: 0.5 if _is_even(i) else 0.0)
    trades = _trades(_is_even)

    feats = causal_features(trades, "W", prices, PAIR)
    assert set(feats) == set(CAUSAL_FEATURE_NAMES)
    assert feats["pdc_5m"] > 0.2


def test_causal_features_zero_without_prices():
    trades = _trades(_is_even)
    feats = causal_features(trades, "W", None, PAIR)
    assert feats == {name: 0.0 for name in CAUSAL_FEATURE_NAMES}


def test_pdc_flows_through_build_feature_vector():
    prices = _prices(lambda i: 0.5 if _is_even(i) else 0.0)
    trades = _trades(_is_even)
    as_of = trades["ledger_close_time"].max()

    feats = build_feature_vector(trades, "W", as_of, prices=prices, pair=PAIR)
    for name in CAUSAL_FEATURE_NAMES:
        assert name in feats
    assert feats["pdc_5m"] > 0.2


# --- risk-score integration -------------------------------------------------


def test_positive_pdc_discounts_risk_score():
    baseline = RiskScore.combine(
        wallet="W",
        asset_pair=PAIR,
        benford_mad=1.0,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
    )
    discounted = RiskScore.combine(
        wallet="W",
        asset_pair=PAIR,
        benford_mad=1.0,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
        pdc_score=0.1,
        pdc_discount_weight=100.0,
    )
    assert discounted.score < baseline.score


def test_negative_pdc_does_not_increase_score():
    # Only positive PDC discounts; a wash-trading (negative) signal must never
    # *raise* the correlational score via this path.
    discounted = RiskScore.combine(
        wallet="W",
        asset_pair=PAIR,
        benford_mad=1.0,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
        pdc_score=-0.5,
        pdc_discount_weight=100.0,
    )
    baseline = RiskScore.combine(
        wallet="W",
        asset_pair=PAIR,
        benford_mad=1.0,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
    )
    assert discounted.score == baseline.score


def test_false_positive_rate_drops_for_high_pdc_wallets():
    """Market makers (clean) with PDC > 0.05 that are false-positives under the
    correlational score should be de-flagged by the causal discount."""
    threshold = 70
    pdcs = [0.06, 0.10, 0.14, 0.18, 0.22, 0.26]

    def score_for(pdc, weight):
        return RiskScore.combine(
            wallet="MM",
            asset_pair=PAIR,
            benford_mad=1.0,
            benford_mad_threshold=0.015,
            ml_probability=0.8,
            ml_confidence=0.9,
            pdc_score=pdc,
            pdc_discount_weight=weight,
        ).score

    baseline_fp = sum(score_for(p, 0.0) >= threshold for p in pdcs)
    adjusted_fp = sum(score_for(p, 100.0) >= threshold for p in pdcs)

    assert baseline_fp > 0
    reduction = (baseline_fp - adjusted_fp) / baseline_fp
    assert reduction >= 0.20


# --- SHAP annotation --------------------------------------------------------


def test_pdc_annotation_directions():
    pos = pdc_annotation("pdc_5m", 0.034)
    assert pos["direction"] == "reduces_risk"
    assert "market making" in pos["interpretation"]

    neg = pdc_annotation("pdc_5m", -0.02)
    assert neg["direction"] == "increases_risk"
    assert "wash trading" in neg["interpretation"]

    zero = pdc_annotation("pdc_5m", 0.0)
    assert zero["direction"] == "neutral"


def test_annotate_causal_features_filters_present_pdc():
    feature_vector = {"pdc_5m": 0.034, "pdc_1h": -0.01, "counterparty_concentration_ratio": 0.5}
    annotations = annotate_causal_features(feature_vector)

    annotated = {a["feature"] for a in annotations}
    assert annotated == {"pdc_5m", "pdc_1h"}
    assert all("interpretation" in a for a in annotations)
