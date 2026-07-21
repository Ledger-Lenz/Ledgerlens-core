"""Prometheus metrics for the LedgerLens detection pipeline."""

from __future__ import annotations

from prometheus_client import (
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

# WAF metrics
ledgerlens_waf_blocks_total = Counter(
    "ledgerlens_waf_blocks_total",
    "Total number of requests blocked by WAF",
    ["rule", "namespace_id"],
)

# Distributed rate limiter metrics (detection/rate_limiter.py)
ledgerlens_rate_limiter_checks_total = Counter(
    "ledgerlens_rate_limiter_checks_total",
    "Total per-key rate limit checks performed, by backend",
    ["backend"],  # "redis" (shared, cross-replica) or "local" (degraded fallback)
)

ledgerlens_rate_limiter_fallback_total = Counter(
    "ledgerlens_rate_limiter_fallback_total",
    "Total rate limit checks served from the in-process fallback because the "
    "shared Redis backend was unavailable (circuit open or a failed call). "
    "Sustained non-zero values mean cross-replica/cross-protocol rate limit "
    "enforcement is degraded to per-process only — see docs/waf_and_rate_limiting.md.",
)


def metrics_response():
    """Return (body_bytes, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
