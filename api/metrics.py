"""Prometheus metrics for the LedgerLens detection pipeline."""

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
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

benford_flags_total = Counter(
    "ledgerlens_benford_flags_total",
    "Total scored wallets whose Benford test flagged an anomaly",
    ["asset_pair"],
)

model_lifecycle_events_total = Counter(
    "ledgerlens_model_lifecycle_events_total",
    "Total model promotion/rollback events (drives dashboard annotations)",
    ["action"],  # "promote" or "rollback"
)


def _chain_submission_backlog() -> float:
    try:
        from detection.chain_submission_queue import queue_stats

        stats = queue_stats()
        return float(stats.get("pending", 0) + stats.get("in_flight", 0))
    except Exception:
        return 0.0


chain_submission_backlog = Gauge(
    "ledgerlens_chain_submission_backlog",
    "On-chain submissions awaiting publication (pending + in_flight)",
)
chain_submission_backlog.set_function(_chain_submission_backlog)

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


ledgerlens_secret_rotation_total = Counter(
    "ledgerlens_secret_rotation_total",
    "Total secret rotation attempts",
    ["secret_type", "result"],
)

def get_overdue_count():
    try:
        from detection.api_key_store import get_overdue_api_keys_count
        return get_overdue_api_keys_count()
    except (ImportError, AttributeError, RuntimeError):
        return 0

ledgerlens_secret_rotation_overdue = Gauge(
    "ledgerlens_secret_rotation_overdue",
    "Number of active API keys that have exceeded their maximum age without rotation",
)
ledgerlens_secret_rotation_overdue.set_function(get_overdue_count)


# Internal event bus dead-letter queue (detection/event_bus.py)
event_bus_dead_lettered_total = Counter(
    "ledgerlens_event_bus_dead_lettered_total",
    "Total risk-score events dead-lettered after exhausting the publish retry budget",
    ["backend"],
)

event_bus_dead_letter_replays_total = Counter(
    "ledgerlens_event_bus_dead_letter_replays_total",
    "Total dead-lettered event replay attempts",
    ["result"],  # "replayed" or "failed"
)


def _event_bus_dlq_stat(stat: str) -> float:
    try:
        from detection.event_bus import get_dead_letter_store

        store = get_dead_letter_store()
        return float(store.count() if stat == "count" else store.oldest_age_seconds())
    except Exception:
        return 0.0


event_bus_dead_letter_events = Gauge(
    "ledgerlens_event_bus_dead_letter_events",
    "Current number of dead-lettered event bus events awaiting replay",
)
event_bus_dead_letter_events.set_function(lambda: _event_bus_dlq_stat("count"))

event_bus_dead_letter_oldest_age_seconds = Gauge(
    "ledgerlens_event_bus_dead_letter_oldest_age_seconds",
    "Age of the oldest dead-lettered event bus event (0 when empty)",
)
event_bus_dead_letter_oldest_age_seconds.set_function(lambda: _event_bus_dlq_stat("age"))


def metrics_response():
    """Return (body_bytes, content_type) for the /metrics endpoint."""
    from prometheus_client import REGISTRY, generate_latest, CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
