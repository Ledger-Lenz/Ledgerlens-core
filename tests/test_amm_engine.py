import pandas as pd

from detection.amm_engine import (
    amm_round_trips_to_alerts,
    detect_profitable_pool_round_trips,
)
from ingestion.data_models import TradeType


def _pool_trade(account, *, buy, price, seconds, pool_id="P1"):
    amount = 100.0
    return {
        "trade_type": TradeType.LIQUIDITY_POOL,
        "liquidity_pool_id": pool_id,
        "base_account": account,
        "base_asset": {"code": "XLM", "issuer": None},
        "counter_asset": {"code": "USDC", "issuer": "GISSUER"},
        "base_amount": amount,
        "counter_amount": amount * price,
        "base_is_seller": not buy,
        "ledger_close_time": pd.Timestamp("2026-06-01T00:00:00Z")
        + pd.Timedelta(seconds=seconds),
    }


def test_detects_profitable_round_trip_after_amm_fees():
    trades = pd.DataFrame(
        [
            _pool_trade("A", buy=True, price=1.0, seconds=0),
            _pool_trade("A", buy=False, price=1.02, seconds=30),
        ]
    )

    anomalies = detect_profitable_pool_round_trips(trades)

    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.wallet == "A"
    assert anomaly.pool_id == "P1"
    assert anomaly.fee_adjusted_profit_quote > 0
    assert anomaly.fee_adjusted_return_ratio > 1
    assert amm_round_trips_to_alerts(anomalies)[0]["detail"]["detection"] == (
        "profitable_amm_round_trip"
    )


def test_rejects_round_trip_that_loses_after_fees():
    trades = pd.DataFrame(
        [
            _pool_trade("A", buy=True, price=1.0, seconds=0),
            _pool_trade("A", buy=False, price=1.005, seconds=30),
        ]
    )

    assert detect_profitable_pool_round_trips(trades) == []


def test_profitable_cycle_must_complete_inside_window():
    trades = pd.DataFrame(
        [
            _pool_trade("A", buy=True, price=1.0, seconds=0),
            _pool_trade("A", buy=False, price=1.02, seconds=3601),
        ]
    )

    assert detect_profitable_pool_round_trips(trades) == []


def test_profitable_cycle_must_use_same_pool():
    trades = pd.DataFrame(
        [
            _pool_trade("A", buy=True, price=1.0, seconds=0, pool_id="P1"),
            _pool_trade("A", buy=False, price=1.02, seconds=30, pool_id="P2"),
        ]
    )

    assert detect_profitable_pool_round_trips(trades) == []
