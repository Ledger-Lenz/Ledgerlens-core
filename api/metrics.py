"""Prometheus metrics for the LedgerLens detection pipeline."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    REGISTRY,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

wallets_scored_total = Counter(
    "ledgerlens_wallets_scored_total",
    "Total wallets scored",
    ["asset_pair", "result"],
)

scoring_latency_seconds = Histogram(
    "ledgerlens_scoring_latency_seconds",
    "Time to score one wallet end-to-end (seconds)",
    ["asset_pair"],
)

soroban_submissions_total = Counter(
    "ledgerlens_soroban_submissions_total",
    "Total Soroban submissions",
    ["status"],
)

soroban_submission_latency_seconds = Histogram(
    "ledgerlens_soroban_submission_latency_seconds",
    "Time for Soroban submit_score() (seconds)",
)

circuit_breaker_open_total = Counter(
    "ledgerlens_circuit_breaker_open_total",
    "Total times the Soroban circuit breaker opened",
)

webhook_deliveries_total = Counter(
    "ledgerlens_webhook_deliveries_total",
    "Total webhook delivery attempts",
    ["result"],
)

drift_detected_total = Counter(
    "ledgerlens_drift_detected_total",
    "Total feature-drift detection events",
)

pipeline_run_duration_seconds = Histogram(
    "ledgerlens_pipeline_run_duration_seconds",
    "Duration of a full pipeline pass (seconds)",
)

api_request_duration_seconds = Histogram(
    "ledgerlens_api_request_duration_seconds",
    "FastAPI request duration (seconds)",
    ["method", "endpoint", "status_code"],
)

model_auc_roc = Gauge(
    "ledgerlens_model_auc_roc",
    "Latest AUC-ROC per model from training metadata",
    ["model_name"],
)


def metrics_response():
    """Return (body_bytes, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
