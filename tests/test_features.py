import pandas as pd

from detection.feature_engineering import (
    FEATURE_NAMES,
    account_age_days,
    benford_features,
    build_feature_vector,
    counterparty_concentration_ratio,
    funding_source_similarity_score,
    intra_minute_clustering_coefficient,
    network_centrality,
    off_hours_activity_ratio,
    order_cancellation_rate,
    round_trip_trade_frequency,
    self_matching_rate,
    volume_spike_frequency,
    volume_to_unique_counterparty_ratio,
)


def _sample_trades() -> pd.DataFrame:
    now = pd.Timestamp("2026-06-12T00:00:00Z")
    return pd.DataFrame(
        [
            {"ledger_close_time": now - pd.Timedelta(minutes=1), "base_account": "A", "counter_account": "B", "base_amount": 100.0},
            {"ledger_close_time": now - pd.Timedelta(minutes=1), "base_account": "A", "counter_account": "B", "base_amount": 100.0},
            {"ledger_close_time": now - pd.Timedelta(minutes=30), "base_account": "A", "counter_account": "C", "base_amount": 50.0},
            {"ledger_close_time": now - pd.Timedelta(hours=2), "base_account": "D", "counter_account": "D", "base_amount": 25.0},
        ]
    )


def test_benford_features_returns_all_windows():
    trades = _sample_trades()
    as_of = pd.Timestamp("2026-06-12T00:00:00Z")
    features = benford_features(trades, as_of)

    for window in ("1h", "4h", "24h", "7d", "30d"):
        assert f"benford_chi_square_{window}" in features
        assert f"benford_mad_{window}" in features
        assert f"benford_max_zscore_{window}" in features


def test_counterparty_concentration_ratio():
    trades = _sample_trades()
    # Account A traded 200 with B and 50 with C -> concentration = 200/250
    assert counterparty_concentration_ratio(trades, "A") == 0.8


def test_self_matching_rate():
    trades = _sample_trades()
    # 1 of 4 trades has base_account == counter_account
    assert self_matching_rate(trades) == 0.25


def test_volume_to_unique_counterparty_ratio():
    trades = _sample_trades()
    # Account A has 250 total volume across 2 unique counterparties (B, C)
    assert volume_to_unique_counterparty_ratio(trades, "A") == 125.0


def test_intra_minute_clustering_coefficient():
    trades = _sample_trades()
    # 2 of 4 trades share the same minute bucket
    assert intra_minute_clustering_coefficient(trades) == 0.5


def test_empty_trades_do_not_error():
    empty = pd.DataFrame(columns=["ledger_close_time", "base_account", "counter_account", "base_amount"])
    as_of = pd.Timestamp("2026-06-12T00:00:00Z")

    assert self_matching_rate(empty) == 0.0
    assert intra_minute_clustering_coefficient(empty) == 0.0
    assert counterparty_concentration_ratio(empty, "A") == 0.0
    assert volume_to_unique_counterparty_ratio(empty, "A") == 0.0
    assert off_hours_activity_ratio(empty) == 0.0
    assert volume_spike_frequency(empty, as_of) == 0.0
    assert round_trip_trade_frequency(empty, "A") == 0.0
    assert network_centrality(empty, "A") == 0.0
    benford_features(empty, as_of)  # should not raise


def test_off_hours_activity_ratio():
    trades = pd.DataFrame(
        [
            {"ledger_close_time": pd.Timestamp("2026-06-12T02:00:00Z"), "base_account": "A", "counter_account": "B", "base_amount": 1.0},
            {"ledger_close_time": pd.Timestamp("2026-06-12T14:00:00Z"), "base_account": "A", "counter_account": "B", "base_amount": 1.0},
        ]
    )
    # 1 of 2 trades occurs in the default 00:00-05:59 UTC off-hours window
    assert off_hours_activity_ratio(trades) == 0.5


def test_volume_spike_frequency_detects_outlier_bucket():
    now = pd.Timestamp("2026-06-12T00:00:00Z")
    rows = [
        {"ledger_close_time": now - pd.Timedelta(hours=h), "base_account": "A", "counter_account": "B", "base_amount": 10.0}
        for h in range(1, 7)
    ]
    rows.append({"ledger_close_time": now - pd.Timedelta(hours=6), "base_account": "A", "counter_account": "B", "base_amount": 1000.0})
    trades = pd.DataFrame(rows)

    assert volume_spike_frequency(trades, now) > 0.0


def test_volume_spike_frequency_flat_volume_is_zero():
    now = pd.Timestamp("2026-06-12T00:00:00Z")
    rows = [
        {"ledger_close_time": now - pd.Timedelta(hours=h), "base_account": "A", "counter_account": "B", "base_amount": 10.0}
        for h in range(1, 7)
    ]
    trades = pd.DataFrame(rows)

    assert volume_spike_frequency(trades, now) == 0.0


def test_round_trip_trade_frequency():
    now = pd.Timestamp("2026-06-12T00:00:00Z")
    xlm = {"code": "XLM", "issuer": None}
    usdc = {"code": "USDC", "issuer": "GISSUER"}

    trades = pd.DataFrame(
        [
            # A gives XLM, gets USDC
            {
                "ledger_close_time": now - pd.Timedelta(minutes=2),
                "base_account": "A",
                "counter_account": "B",
                "base_amount": 100.0,
                "counter_amount": 10.0,
                "base_asset": xlm,
                "counter_asset": usdc,
            },
            # A (as counter) gives USDC back, gets XLM back -> reverses the first trade
            {
                "ledger_close_time": now - pd.Timedelta(minutes=1),
                "base_account": "B",
                "counter_account": "A",
                "base_amount": 100.0,
                "counter_amount": 10.0,
                "base_asset": xlm,
                "counter_asset": usdc,
            },
        ]
    )

    # 1 of A's 2 trades is the start of a round trip
    assert round_trip_trade_frequency(trades, "A") == 0.5


def test_network_centrality():
    trades = pd.DataFrame(
        [
            {"base_account": "A", "counter_account": "B"},
            {"base_account": "A", "counter_account": "C"},
            {"base_account": "D", "counter_account": "D"},
        ]
    )
    # A has 2 unique counterparties (B, C) out of 3 other accounts (B, C, D)
    assert network_centrality(trades, "A") == 2 / 3


def test_funding_source_similarity_score():
    trades = pd.DataFrame(
        [
            {"base_account": "A", "counter_account": "B"},
            {"base_account": "A", "counter_account": "C"},
        ]
    )
    account_metadata = {
        "A": {"funding_source": "F1"},
        "B": {"funding_source": "F1"},
        "C": {"funding_source": "F2"},
    }
    # 1 of A's 2 counterparties (B) shares A's funding source
    assert funding_source_similarity_score(trades, "A", account_metadata) == 0.5


def test_funding_source_similarity_score_unknown_account():
    trades = pd.DataFrame([{"base_account": "A", "counter_account": "B"}])
    assert funding_source_similarity_score(trades, "A", {}) == 0.0


def test_account_age_days():
    as_of = pd.Timestamp("2026-06-12T00:00:00Z")
    account_metadata = {"A": {"created_at": pd.Timestamp("2026-06-01T00:00:00Z")}}

    assert account_age_days("A", as_of, account_metadata) == 11.0
    assert account_age_days("B", as_of, account_metadata) == 0.0


def test_order_cancellation_rate():
    events = pd.DataFrame(
        [
            {"account": "A", "event_type": "created"},
            {"account": "A", "event_type": "cancelled"},
            {"account": "A", "event_type": "cancelled"},
            {"account": "B", "event_type": "created"},
        ]
    )
    assert order_cancellation_rate(events, "A") == 2 / 3
    assert order_cancellation_rate(events, "C") == 0.0

    empty = pd.DataFrame(columns=["account", "event_type"])
    assert order_cancellation_rate(empty, "A") == 0.0


def test_build_feature_vector_returns_all_feature_names():
    trades = _sample_trades()
    trades["base_asset"] = [{"code": "XLM", "issuer": None}] * len(trades)
    trades["counter_asset"] = [{"code": "USDC", "issuer": "GISSUER"}] * len(trades)
    as_of = pd.Timestamp("2026-06-12T00:00:00Z")

    features = build_feature_vector(trades, "A", as_of)

    assert set(features.keys()) == set(FEATURE_NAMES)
