"""Tests for detection.compliance_report."""

import os
import tempfile

from detection.compliance_report import (
    ComplianceReportData,
    ComplianceReportGenerator,
    FeatureAttribution,
    BenfordSummary,
    generate_report_data,
    render_html,
)


def _sample_data() -> ComplianceReportData:
    return generate_report_data(
        wallet="GBZX4364PWQBQFXBQ5QI5Q4WNZKEMQFZAERIWVEBK5PL7RXDSE4BVNP",
        date="2026-06-01",
        score_record={"score": 82, "confidence": 90, "ci_low": 75, "ci_high": 89},
        feature_vector={
            "trade_count": 150,
            "self_trade_ratio": 0.42,
            "round_trip_ratio": 0.31,
            "counterparty_concentration": 0.88,
            "benford_mad": 0.025,
        },
        shap_values={
            "trade_count": 0.12,
            "self_trade_ratio": 0.45,
            "round_trip_ratio": 0.33,
            "counterparty_concentration": -0.15,
            "benford_mad": 0.08,
        },
        benford_metrics={"chi2": 22.5, "mad": 0.025, "flag": True},
        trades=[
            {
                "ledger_close_time": "2026-06-01T12:00:00Z",
                "base_asset": "XLM",
                "counter_asset": "USDC",
                "base_amount": 100.0,
                "price": 0.12,
                "trade_type": "orderbook",
            }
        ],
        model_metadata={"version": "v1.2.3", "training_date": "2026-05-15"},
    )


def test_generate_report_data():
    data = _sample_data()
    assert data.wallet.startswith("G")
    assert data.score == 82
    assert data.risk_level == "High"
    assert len(data.top_features) == 5
    assert data.benford.flag == "True"
    assert data.report_id


def test_render_html():
    data = _sample_data()
    html = render_html(data)
    assert "<!DOCTYPE html>" in html
    assert data.wallet in html
    assert "Risk Score" in html


def test_idempotent_report_id():
    d1 = generate_report_data(
        wallet="GBZX4364PWQBQFXBQ5QI5Q4WNZKEMQFZAERIWVEBK5PL7RXDSE4BVNP",
        date="2026-06-01",
    )
    d2 = generate_report_data(
        wallet="GBZX4364PWQBQFXBQ5QI5Q4WNZKEMQFZAERIWVEBK5PL7RXDSE4BVNP",
        date="2026-06-01",
    )
    assert d1.report_id == d2.report_id


def test_low_risk_summary():
    data = generate_report_data(
        wallet="GBZX4364PWQBQFXBQ5QI5Q4WNZKEMQFZAERIWVEBK5PL7RXDSE4BVNP",
        date="2026-06-01",
        score_record={"score": 20, "confidence": 95},
    )
    assert data.risk_level == "Low"
    assert "normal" in data.summary_text.lower()


def test_medium_risk_summary():
    data = generate_report_data(
        wallet="GBZX4364PWQBQFXBQ5QI5Q4WNZKEMQFZAERIWVEBK5PL7RXDSE4BVNP",
        date="2026-06-01",
        score_record={"score": 60, "confidence": 80},
    )
    assert data.risk_level == "Medium"
