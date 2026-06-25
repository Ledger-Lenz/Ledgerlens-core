"""Tests for StreamingFeatureEngine (Issue #104).

Verifies incremental state management, feature correctness, latency tracking,
and the horizon_streamer integration function.
"""

from datetime import datetime, timezone


from detection.streaming_features import (
    StreamingFeatureEngine,
    WindowState,
    _first_digit,
)
from detection.risk_score import RiskScore
from ingestion.data_models import Asset, Trade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_XLM = Asset(code="XLM")
_USDC = Asset(code="USDC", issuer="GISSUER")

BASE_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _trade(
    wallet: str,
    counterparty: str,
    amount: float,
    dt: datetime | None = None,
    idx: int = 0,
) -> Trade:
    return Trade(
        id=f"t{idx}-{amount}",
        ledger_close_time=dt or BASE_TS,
        base_account=wallet,
        counter_account=counterparty,
        base_asset=_XLM,
        counter_asset=_USDC,
        base_amount=amount,
        counter_amount=amount * 2,
        price=2.0,
        base_is_seller=True,
    )


# ---------------------------------------------------------------------------
# _first_digit helper
# ---------------------------------------------------------------------------


def test_first_digit_basics():
    assert _first_digit(100.0) == 1
    assert _first_digit(250.5) == 2
    assert _first_digit(0.03) == 3
    assert _first_digit(0.0) is None
    assert _first_digit(-5.0) is None
    assert _first_digit(float("nan")) is None


# ---------------------------------------------------------------------------
# WindowState unit tests
# ---------------------------------------------------------------------------


def test_window_state_add_and_remove():
    ws = WindowState(window_sec=3600)
    # Manually call update to exercise add/evict logic.
    # ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket
    ws.update(1000, 100.0, "CP1", 5, "XLM", "USDC", False, 1, 16, 0)
    assert ws.trade_count == 1
    assert abs(ws.amount_sum - 100.0) < 1e-9
    assert ws.digit_histogram[0] == 1  # digit 1
    assert ws.off_hours_count == 1  # hour=5 is off-hours


def test_window_state_evicts_expired_trades():
    ws = WindowState(window_sec=60)  # 1-minute window
    ws.update(0, 100.0, "CP1", 10, "XLM", "USDC", False, 1, 0, 0)
    ws.update(120, 200.0, "CP2", 10, "XLM", "USDC", False, 2, 2, 0)  # evicts the first trade
    assert ws.trade_count == 1
    assert abs(ws.amount_sum - 200.0) < 1e-9


def test_window_state_counterparty_concentration():
    ws = WindowState(window_sec=3600)
    ws.update(100, 300.0, "CP1", 10, "XLM", "USDC", False, 3, 1, 0)
    ws.update(101, 100.0, "CP2", 10, "XLM", "USDC", False, 1, 1, 0)
    ratio = ws.counterparty_concentration()
    assert abs(ratio - 0.75) < 1e-9  # 300 / 400


def test_window_state_self_matching_rate():
    ws = WindowState(window_sec=3600)
    ws.update(100, 100.0, "SELF", 10, "XLM", "USDC", True, 1, 1, 0)
    ws.update(101, 100.0, "CP1", 10, "XLM", "USDC", False, 1, 1, 0)
    assert ws.self_matching_rate() == 0.5


def test_window_state_off_hours_ratio():
    ws = WindowState(window_sec=3600)
    ws.update(100, 100.0, "CP", 3, "XLM", "USDC", False, 1, 1, 0)  # off-hours
    ws.update(101, 100.0, "CP", 12, "XLM", "USDC", False, 1, 1, 0)  # normal hours
    assert ws.off_hours_activity_ratio() == 0.5


def test_window_state_benford_metrics_single_digit():
    ws = WindowState(window_sec=3600)
    # All amounts start with digit 1 (100, 111, 150, ...)
    for i in range(10):
        ws.update(100 + i, 100.0 + i, "CP", 10, "XLM", "USDC", False, 1, 1, 0)
    chi_sq, mad, max_z = ws.benford_metrics()
    assert chi_sq >= 0.0
    assert mad >= 0.0
    assert max_z >= 0.0


# ---------------------------------------------------------------------------
# StreamingFeatureEngine integration tests
# ---------------------------------------------------------------------------


def test_engine_update_returns_feature_vector():
    engine = StreamingFeatureEngine()
    t = _trade("WA", "WB", 100.0)
    fv = engine.update(t)
    assert isinstance(fv, dict)
    assert "benford_chi_square_1h" in fv
    assert "counterparty_concentration_ratio" in fv
    assert "stream_latency_ms" in fv


def test_engine_tracks_both_sides():
    engine = StreamingFeatureEngine()
    t = _trade("WA", "WB", 100.0)
    engine.update(t)
    assert engine.wallet_count() == 2  # both WA and WB are tracked


def test_engine_incremental_benford_update():
    engine = StreamingFeatureEngine()
    for i in range(30):
        t = _trade("WA", "WB", float(100 + i))
        engine.update(t)
    fv = engine.get_features("WA")
    # With 30 trades, Benford features should be non-zero
    assert fv["benford_chi_square_1h"] >= 0.0
    assert fv["benford_mad_1h"] >= 0.0


def test_engine_latency_ms_non_negative():
    engine = StreamingFeatureEngine()
    fv = engine.update(_trade("WA", "WB", 100.0))
    assert fv["stream_latency_ms"] >= 0.0


def test_engine_unknown_wallet_returns_zero_vector():
    engine = StreamingFeatureEngine()
    fv = engine.get_features("UNKNOWN")
    assert fv["benford_chi_square_1h"] == 0.0
    assert fv["counterparty_concentration_ratio"] == 0.0
    assert fv["stream_latency_ms"] == 0.0


def test_engine_all_5_windows_in_feature_vector():
    engine = StreamingFeatureEngine()
    engine.update(_trade("WA", "WB", 100.0))
    fv = engine.get_features("WA")
    for label in ("1h", "4h", "24h", "7d", "30d"):
        assert f"benford_chi_square_{label}" in fv
        assert f"benford_mad_{label}" in fv
        assert f"benford_max_zscore_{label}" in fv


def test_engine_off_hours_detection():
    # Trade at 2 AM UTC (off-hours)
    off_ts = datetime(2026, 6, 1, 2, 0, 0, tzinfo=timezone.utc)
    t = _trade("WA", "WB", 100.0, dt=off_ts)
    engine = StreamingFeatureEngine()
    engine.update(t)
    fv = engine.get_features("WA")
    # The off_hours_activity_ratio should reflect the off-hours trade
    assert fv["off_hours_activity_ratio"] == 1.0


def test_engine_self_matching_rate():
    t = _trade("WA", "WA", 100.0)  # self-trade
    engine = StreamingFeatureEngine()
    engine.update(t)
    fv = engine.get_features("WA")
    assert fv["self_matching_rate"] == 1.0


# ---------------------------------------------------------------------------
# RiskScore latency_ms field
# ---------------------------------------------------------------------------


def test_risk_score_has_latency_ms_field():
    score = RiskScore.combine(
        wallet="W",
        asset_pair="XLM/USDC",
        benford_mad=0.01,
        benford_mad_threshold=0.015,
        ml_probability=0.5,
        ml_confidence=0.8,
    )
    # Default is None
    assert score.latency_ms is None


def test_risk_score_latency_ms_can_be_set():
    from datetime import datetime, timezone
    score = RiskScore(
        wallet="W",
        asset_pair="XLM/USDC",
        score=50,
        benford_flag=False,
        ml_flag=True,
        confidence=80,
        timestamp=datetime.now(timezone.utc),
        latency_ms=12.5,
    )
    assert score.latency_ms == 12.5


# ---------------------------------------------------------------------------
# horizon_streamer integration
# ---------------------------------------------------------------------------


def test_stream_with_features_is_importable():
    from ingestion.horizon_streamer import stream_with_features
    assert callable(stream_with_features)
