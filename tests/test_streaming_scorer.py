"""Tests for streaming scorer uncertainty integration.

Verifies that uncertainty fields (``score_lower``, ``score_upper``) are
included in the alert payload when conformal calibration artifacts are
present, and that the system falls back gracefully without them.
"""

from datetime import datetime
from unittest.mock import MagicMock

from detection.feature_engineering import FEATURE_NAMES
from detection.risk_score import RiskScore
from ingestion.data_models import Asset, Trade
from tests.factories import TradeFactory


def _make_trade(
    base_account: str,
    counter_account: str | None,
    idx: int = 0,
    ts: datetime | None = None,
) -> Trade:
    return TradeFactory.trade(
        id=f"trade-{idx}",
        ledger_close_time=ts or datetime(2024, 1, 1, 12, 0, 0),
        base_account=base_account,
        counter_account=counter_account,
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(
            code="USDC",
            issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
        ),
        base_amount=100.0,
        counter_amount=200.0,
        price=2.0,
        base_is_seller=True,
    )


def test_enqueue_webhook_alerts_includes_uncertainty(monkeypatch):
    """When scores have uncertainty fields, the enqueued alert payload
    must contain score_lower and score_upper."""
    import run_pipeline

    enqueued_payloads = []

    def mock_enqueue(subscriber_id, payload):
        enqueued_payloads.append(payload)

    monkeypatch.setattr(
        "detection.webhook_queue.enqueue", mock_enqueue
    )
    monkeypatch.setattr(
        "detection.webhook_registry.get_matching_subscribers",
        lambda score: [MagicMock(subscriber_id="sub-1")],
    )
    monkeypatch.setattr(
        "detection.webhook_registry.init_db", lambda: None
    )
    monkeypatch.setattr(
        "detection.webhook_queue.init_db", lambda: None
    )

    scores = [
        RiskScore.combine(
            wallet="GAAA",
            asset_pair="XLM/USDC",
            benford_mad=0.001,
            benford_mad_threshold=0.015,
            ml_probability=0.3,
            ml_confidence=0.8,
            score_lower=15.0,
            score_upper=45.0,
            prediction_set=[0, 1],
            coverage_guarantee=0.90,
        )
    ]

    run_pipeline._enqueue_webhook_alerts(scores)

    assert len(enqueued_payloads) >= 1
    payload = enqueued_payloads[0]
    assert payload.get("score_lower") == 15.0
    assert payload.get("score_upper") == 45.0


def test_flush_streaming_buffer_with_calibration(monkeypatch):
    """_flush_streaming_buffer must not crash when calibrators are provided."""
    import run_pipeline

    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})
    monkeypatch.setattr(
        run_pipeline, "build_feature_vector", lambda *a, **kw: {name: 0.0 for name in FEATURE_NAMES}
    )
    monkeypatch.setattr(run_pipeline, "record_scored_features", lambda *a, **kw: None)
    monkeypatch.setattr(run_pipeline, "save_scores", lambda *a, **kw: None)

    buffer = [_make_trade("GAAA", "GBBB", 0), _make_trade("GCCC", "GDDD", 1)]

    models = {
        "random_forest": MagicMock(),
        "xgboost": MagicMock(),
        "lightgbm": MagicMock(),
    }
    calibrators = {
        "random_forest": MagicMock(q_hat=0.15, alpha=0.10),
        "xgboost": MagicMock(q_hat=0.15, alpha=0.10),
        "lightgbm": MagicMock(q_hat=0.15, alpha=0.10),
    }

    # This should not crash
    run_pipeline._flush_streaming_buffer(
        buffer, models, "XLM/USDC", (None, "USDC:..."), "cursor-1", calibrators
    )
