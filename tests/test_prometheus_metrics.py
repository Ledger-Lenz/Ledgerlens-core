"""Tests for Prometheus metrics."""

import re
import pytest
from prometheus_client import REGISTRY, generate_latest


def _get_counter_value(metric_name: str, labels: dict) -> float:
    for metric in REGISTRY.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if sample.name in (metric_name + "_total", metric_name):
                    if all(sample.labels.get(k) == v for k, v in labels.items()):
                        return sample.value
    return 0.0


def test_wallets_scored_total_increments():
    from api.metrics import wallets_scored_total

    before = _get_counter_value("ledgerlens_wallets_scored", {"asset_pair": "XLM/TEST", "result": "above_threshold"})
    wallets_scored_total.labels(asset_pair="XLM/TEST", result="above_threshold").inc()
    after = _get_counter_value("ledgerlens_wallets_scored", {"asset_pair": "XLM/TEST", "result": "above_threshold"})
    assert after == before + 1


def test_soroban_submissions_total_increments():
    from api.metrics import soroban_submissions_total

    before = _get_counter_value("ledgerlens_soroban_submissions", {"status": "submitted"})
    soroban_submissions_total.labels(status="submitted").inc()
    after = _get_counter_value("ledgerlens_soroban_submissions", {"status": "submitted"})
    assert after == before + 1


def test_metrics_endpoint_returns_200_with_content():
    from fastapi import FastAPI
    from fastapi.responses import Response
    from starlette.testclient import TestClient
    from api.metrics import metrics_response

    test_app = FastAPI()

    @test_app.get("/metrics")
    def _metrics():
        body, ct = metrics_response()
        return Response(content=body, media_type=ct)

    client = TestClient(test_app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"ledgerlens_wallets_scored_total" in resp.content


def test_no_wallet_address_in_metric_labels():
    stellar_pattern = re.compile(r"G[A-Z2-7]{55}")
    output = generate_latest(REGISTRY).decode()
    for line in output.splitlines():
        assert not stellar_pattern.search(line), f"Stellar wallet address in metrics: {line}"


def test_all_10_metrics_registered():
    import api.metrics  # ensure module imported
    output = generate_latest(REGISTRY).decode()
    expected = [
        "ledgerlens_wallets_scored_total",
        "ledgerlens_scoring_latency_seconds",
        "ledgerlens_soroban_submissions_total",
        "ledgerlens_soroban_submission_latency_seconds",
        "ledgerlens_circuit_breaker_open_total",
        "ledgerlens_webhook_deliveries_total",
        "ledgerlens_drift_detected_total",
        "ledgerlens_pipeline_run_duration_seconds",
        "ledgerlens_api_request_duration_seconds",
        "ledgerlens_model_auc_roc",
    ]
    for name in expected:
        assert name in output, f"{name!r} missing from /metrics output"
