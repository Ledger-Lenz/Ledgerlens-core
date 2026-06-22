"""Tests for the AMM sandwich-attack detector and its integrations."""

import random

import pandas as pd

from detection.amm_engine import pool_sandwich_count, pool_sandwich_frequency
from detection.feature_engineering import (
    SANDWICH_FEATURE_NAMES,
    build_feature_vector,
    sandwich_features,
)
from detection.risk_score import RiskScore
from detection.sandwich_engine import SandwichCandidate, detect_sandwich_candidates
from detection.storage import (
    AlertType,
    get_alerts,
    sandwich_candidates_to_alerts,
    save_alerts,
)
from ingestion.data_models import TradeType

XLM = {"code": "XLM", "issuer": None}
USDC = {"code": "USDC", "issuer": "GISSUER"}
BASE_TS = pd.Timestamp("2026-06-01T00:00:00Z")


def _trade(
    *,
    account,
    is_seller,
    price,
    amount,
    ledger,
    op,
    pool_id="P1",
    seconds=None,
):
    return {
        "id": f"{account}-{ledger}-{op}",
        "ledger_close_time": BASE_TS + pd.Timedelta(seconds=seconds if seconds is not None else ledger),
        "base_account": account,
        "counter_account": None,
        "base_asset": XLM,
        "counter_asset": USDC,
        "base_amount": amount,
        "counter_amount": amount * price,
        "price": price,
        "base_is_seller": is_seller,
        "trade_type": TradeType.LIQUIDITY_POOL,
        "liquidity_pool_id": pool_id,
        "ledger_sequence": ledger,
        "operation_order": op,
    }


def _sandwich_frame():
    """A single clean sandwich: attacker A buys, victim V buys, attacker A sells."""
    return pd.DataFrame(
        [
            _trade(account="A", is_seller=False, price=1.00, amount=1000, ledger=1, op=0),
            _trade(account="V", is_seller=False, price=1.05, amount=500, ledger=1, op=1),
            _trade(account="A", is_seller=True, price=1.10, amount=1000, ledger=1, op=2),
        ]
    )


def test_detects_known_sandwich():
    candidates = detect_sandwich_candidates(_sandwich_frame())

    assert len(candidates) == 1
    c = candidates[0]
    assert isinstance(c, SandwichCandidate)
    assert c.attacker == "A"
    assert c.victim == "V"
    assert c.pool_id == "P1"
    assert c.buy_op_idx == 0
    assert c.victim_op_idx == 1
    assert c.sell_op_idx == 2
    assert c.ledger_sequence == 1
    # (1.10 - 1.00) * 1000 = 100 XLM
    assert c.profit_xlm == 100.0
    # (1.05 - 1.00) / 1.00 = 0.05
    assert c.slippage_inflicted == 0.05


def test_no_sandwich_without_victim():
    df = _sandwich_frame()
    df = df[df["base_account"] != "V"]  # drop the victim leg
    assert detect_sandwich_candidates(df) == []


def test_no_sandwich_when_price_not_inflated():
    df = _sandwich_frame()
    # attacker sells at the same price it bought -> no profit, not a sandwich
    df.loc[df["operation_order"] == 2, "price"] = 1.00
    assert detect_sandwich_candidates(df) == []


def test_min_profit_threshold_filters_small_sandwiches():
    df = _sandwich_frame()
    assert len(detect_sandwich_candidates(df, min_profit_xlm=10.0)) == 1
    # profit is 100 XLM; a 500 XLM floor rejects it
    assert detect_sandwich_candidates(df, min_profit_xlm=500.0) == []


def test_attacker_legs_must_share_account():
    df = _sandwich_frame()
    # the closing sell is now a different account -> no shared attacker
    df.loc[df["operation_order"] == 2, "base_account"] = "B"
    assert detect_sandwich_candidates(df) == []


def test_max_ledger_gap_enforced():
    df = pd.DataFrame(
        [
            _trade(account="A", is_seller=False, price=1.00, amount=1000, ledger=1, op=0),
            _trade(account="V", is_seller=False, price=1.05, amount=500, ledger=2, op=0),
            _trade(account="A", is_seller=True, price=1.10, amount=1000, ledger=9, op=0),
        ]
    )
    assert detect_sandwich_candidates(df, max_ledger_gap=2) == []
    assert len(detect_sandwich_candidates(df, max_ledger_gap=8)) == 1


def test_ordering_derived_from_close_time_when_columns_absent():
    df = _sandwich_frame().drop(columns=["ledger_sequence", "operation_order"])
    candidates = detect_sandwich_candidates(df)
    assert len(candidates) == 1
    assert candidates[0].attacker == "A"


def test_false_positive_rate_below_half_percent_on_benign_data():
    """Benign pool trades (distinct accounts, random-walk prices) should almost
    never be flagged. Approximates the <0.5% FP acceptance criterion."""
    rng = random.Random(1234)
    rows = []
    price = 1.0
    for i in range(2000):
        price *= 1 + rng.uniform(-0.002, 0.002)
        rows.append(
            _trade(
                account=f"W{i}",  # every trade a distinct account -> no attacker round-trips
                is_seller=rng.random() < 0.5,
                price=price,
                amount=rng.uniform(10, 1000),
                ledger=i // 4,
                op=i % 4,
            )
        )
    df = pd.DataFrame(rows)
    candidates = detect_sandwich_candidates(df)
    assert len(candidates) / len(df) < 0.005


def test_amm_engine_pool_helpers():
    df = _sandwich_frame()
    assert pool_sandwich_count(df, "P1") == 1
    assert pool_sandwich_count(df, "UNKNOWN") == 0
    # 3 legs out of 3 trades -> clamped to 1.0
    assert pool_sandwich_frequency(df, "P1") == 1.0


def test_sandwich_features_present_and_populated():
    df = _sandwich_frame()
    as_of = df["ledger_close_time"].max()

    feats = sandwich_features(df, "A", as_of)
    assert set(feats) == set(SANDWICH_FEATURE_NAMES)
    assert feats["sandwich_profit_xlm_30d"] == 100.0
    assert feats["sandwich_ratio"] > 0.0

    # the victim is not an aggressor
    victim_feats = sandwich_features(df, "V", as_of)
    assert victim_feats["sandwich_profit_xlm_30d"] == 0.0


def test_sandwich_features_flow_into_full_vector():
    df = _sandwich_frame()
    as_of = df["ledger_close_time"].max()
    feats = build_feature_vector(df, "A", as_of)
    for name in SANDWICH_FEATURE_NAMES:
        assert name in feats
    assert feats["sandwich_profit_xlm_30d"] == 100.0


def test_sandwich_signal_flows_into_risk_score():
    baseline = RiskScore.combine(
        wallet="A",
        asset_pair="XLM/USDC",
        benford_mad=0.0,
        benford_mad_threshold=0.015,
        ml_probability=0.0,
        ml_confidence=0.5,
    )
    assert baseline.score == 0

    boosted = RiskScore.combine(
        wallet="A",
        asset_pair="XLM/USDC",
        benford_mad=0.0,
        benford_mad_threshold=0.015,
        ml_probability=0.0,
        ml_confidence=0.5,
        sandwich_signal=1.0,
        sandwich_weight=0.5,
    )
    assert boosted.score == 50


def test_sandwich_alerts_stored_and_retrieved(tmp_path):
    db_path = str(tmp_path / "alerts.db")
    candidates = detect_sandwich_candidates(_sandwich_frame())
    alerts = sandwich_candidates_to_alerts(candidates, asset_pair="XLM/USDC")
    save_alerts(alerts, db_path=db_path)

    stored = get_alerts(alert_type=AlertType.SANDWICH_ATTACK, db_path=db_path)
    assert len(stored) == 1
    alert = stored[0]
    assert alert["alert_type"] == "SANDWICH_ATTACK"
    assert alert["wallet"] == "A"
    assert alert["asset_pair"] == "XLM/USDC"
    assert alert["pool_id"] == "P1"
    assert alert["detail"]["victim"] == "V"
    assert alert["detail"]["profit_xlm"] == 100.0

    # filtering by an unrelated type returns nothing
    assert get_alerts(alert_type=AlertType.WASH_TRADING, db_path=db_path) == []


def test_alerts_endpoint_returns_sandwich_alerts(tmp_path, monkeypatch):
    import base64
    import os

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "api.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    monkeypatch.setenv(
        "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", db_path)

    candidates = detect_sandwich_candidates(_sandwich_frame())
    save_alerts(sandwich_candidates_to_alerts(candidates, asset_pair="XLM/USDC"), db_path=db_path)

    from api.main import app

    client = TestClient(app)
    resp = client.get("/alerts?alert_type=SANDWICH_ATTACK")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["alert_type"] == "SANDWICH_ATTACK"
    assert body[0]["wallet"] == "A"
