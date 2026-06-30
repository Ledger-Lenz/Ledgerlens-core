"""Tests for Temporal Pattern Analysis Engine (Issue #298)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trade(seconds_offset: float, amount: float = 100.0):
    t = MagicMock()
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t.ledger_close_time = base_ts + timedelta(seconds=seconds_offset)
    t.base_amount = amount
    t.base_account = "G" + "A" * 55
    t.counter_account = "G" + "B" * 55
    t.base_asset_code = "XLM"
    t.counter_asset_code = "USDC"
    return t


def _evenly_spaced_trades(n: int, interval_secs: float = 60.0) -> list:
    return [_make_trade(i * interval_secs, amount=50.0) for i in range(n)]


# ---------------------------------------------------------------------------
# Tests: TradeTimeSeries
# ---------------------------------------------------------------------------


def test_trade_time_series_bin_counts():
    """100 evenly spaced trades produce correct bin counts and near-zero iat_cv."""
    from detection.temporal_patterns import TradeTimeSeries

    # 100 trades every 60 seconds = 100 minutes → 20 bins at 5-min resolution
    trades = _evenly_spaced_trades(100, interval_secs=60.0)
    ts = TradeTimeSeries.from_trades("G" + "A" * 55, trades, window_hours=2)

    # At least some bins should be non-zero
    assert np.sum(ts.trade_count_series) == 100
    # Evenly spaced → iat_cv should be very low (< 0.1)
    assert ts.iat_cv < 0.1


def test_trade_time_series_iat_cv_single_trade():
    """iat_cv returns 1.0 when fewer than 2 trades are present."""
    from detection.temporal_patterns import TradeTimeSeries

    ts = TradeTimeSeries.from_trades("G" + "A" * 55, [_make_trade(0)], window_hours=24)
    assert ts.iat_cv == 1.0


def test_trade_time_series_empty():
    """Empty trade list produces zero-filled series without error."""
    from detection.temporal_patterns import TradeTimeSeries

    ts = TradeTimeSeries.from_trades("G" + "A" * 55, [], window_hours=24)
    assert np.all(ts.log_amount_series == 0)
    assert len(ts.iat_series) == 0
    assert ts.iat_cv == 1.0


# ---------------------------------------------------------------------------
# Tests: ARIMAResidualDetector
# ---------------------------------------------------------------------------


def test_arima_score_returns_zero_when_not_fitted():
    """ARIMAResidualDetector.score returns 0.0 when not fitted."""
    from detection.temporal_patterns import ARIMAResidualDetector

    detector = ARIMAResidualDetector()
    series = np.random.randn(400).astype(np.float32)
    assert detector.score(series) == 0.0


def test_arima_score_in_range():
    """ARIMAResidualDetector.score returns a value in [0, 1] for a non-degenerate series."""
    pytest.importorskip("statsmodels")
    from detection.temporal_patterns import ARIMAResidualDetector

    rng = np.random.default_rng(42)
    series = rng.normal(loc=2.0, scale=0.5, size=400).astype(np.float32)

    detector = ARIMAResidualDetector(score_window_steps=50)
    detector.fit(series)
    score = detector.score(series)
    assert 0.0 <= score <= 1.0


def test_arima_handles_constant_series():
    """ARIMA on all-zero series does not raise; returns 0.0."""
    pytest.importorskip("statsmodels")
    from detection.temporal_patterns import ARIMAResidualDetector

    series = np.zeros(400, dtype=np.float32)
    detector = ARIMAResidualDetector(score_window_steps=50)
    detector.fit(series)
    assert detector.score(series) == 0.0


# ---------------------------------------------------------------------------
# Tests: LSTMAutoencoder
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("torch"),
    reason="torch not installed",
)
def test_lstm_forward_pass_shape():
    """LSTMAutoencoder forward pass produces output shape matching input."""
    import torch
    from detection.temporal_patterns import LSTMAutoencoder

    model = LSTMAutoencoder(input_dim=2, hidden_dim=16, num_layers=2, sequence_length=48)
    x = torch.randn(4, 48, 2)  # batch=4, seq=48, features=2
    out = model(x)
    assert out.shape == (4, 48, 2)


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("torch"),
    reason="torch not installed",
)
def test_lstm_reconstruction_loss_range():
    """LSTMAutoencoder.reconstruction_loss returns value in [0, 1]."""
    import torch
    from detection.temporal_patterns import LSTMAutoencoder

    model = LSTMAutoencoder(input_dim=2, hidden_dim=16, num_layers=2, sequence_length=48)
    x = torch.randn(1, 48, 2)
    loss = model.reconstruction_loss(x)
    assert 0.0 <= loss <= 1.0


# ---------------------------------------------------------------------------
# Tests: BurstSynchronyDetector
# ---------------------------------------------------------------------------


def test_synchrony_single_wallet():
    """synchrony_score returns 0.0 for a single wallet."""
    from detection.temporal_patterns import BurstSynchronyDetector

    detector = BurstSynchronyDetector()
    series = {"wallet_a": np.array([1.0, 2.0, 0.0, 3.0])}
    assert detector.synchrony_score(series) == 0.0


def test_synchrony_two_identical_series():
    """Two perfectly correlated series → synchrony_score near 1.0."""
    from detection.temporal_patterns import BurstSynchronyDetector

    detector = BurstSynchronyDetector()
    a = np.array([0.0, 5.0, 0.0, 5.0, 0.0], dtype=np.float64)
    series = {"wallet_a": a, "wallet_b": a.copy()}
    score = detector.synchrony_score(series)
    assert score > 0.95


def test_synchrony_all_zero_series():
    """All-zero series return 0.0 (no synchrony signal)."""
    from detection.temporal_patterns import BurstSynchronyDetector

    detector = BurstSynchronyDetector()
    z = np.zeros(10, dtype=np.float64)
    assert detector.synchrony_score({"a": z, "b": z}) == 0.0


# ---------------------------------------------------------------------------
# Tests: TemporalPatternScorer
# ---------------------------------------------------------------------------


def test_temporal_scorer_weight_sum():
    """TemporalPatternScorer constructor asserts weights sum to 1.0."""
    from detection.temporal_patterns import TemporalPatternScorer

    # Valid
    scorer = TemporalPatternScorer(0.3, 0.3, 0.2, 0.2)
    assert scorer is not None

    # Invalid — should assert
    with pytest.raises(AssertionError):
        TemporalPatternScorer(0.5, 0.5, 0.5, 0.5)


def test_temporal_scorer_iat_low_cv():
    """Low iat_cv (metronomic) → high iat_score contribution."""
    from detection.temporal_patterns import TemporalPatternScorer

    scorer = TemporalPatternScorer(0.0, 0.0, 1.0, 0.0)
    score = scorer.score(0.0, 0.0, iat_cv=0.0, synchrony=0.0)
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tests: Minimum trades guard
# ---------------------------------------------------------------------------


def test_min_trades_guard_returns_zero():
    """Wallets with fewer trades than min_trades get temporal_anomaly_score=0.0."""
    from detection.temporal_patterns import score_wallet_temporal

    # Only 5 trades (below default min of 10)
    trades = _evenly_spaced_trades(5)
    result = score_wallet_temporal(
        wallet="G" + "A" * 55,
        trades=trades,
        min_trades=10,
    )
    assert result["temporal_anomaly_score"] == 0.0
    assert result["arima_residual_score"] == 0.0
    assert result["iat_variance_score"] == 0.0
