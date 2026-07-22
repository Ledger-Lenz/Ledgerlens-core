# LedgerLens Observability Stack

This document covers structured logging, correlation ID propagation, OpenTelemetry tracing, Prometheus metrics, alerting rules, and the wallet masking policy for `ledgerlens-core`.

---

## Structured JSON Logging

All log output is JSON (via [structlog](https://www.structlog.org/)). Every record includes:

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC timestamp |
| `level` | Log level (`info`, `warning`, `error`) |
| `logger` | Logger name (e.g. `ledgerlens.pipeline`) |
| `event` | Log message |
| `correlation_id` | Correlation ID for the current request/pipeline pass |
| `trace_id` | OpenTelemetry trace ID (32-hex chars; `000...0` when no active span) |

**Initialise** once per process entry point:

```python
from config.logging_config import configure_logging
configure_logging("ledgerlens-api")  # or "ledgerlens-cli", "ledgerlens-pipeline"
```

This replaces the root logger's handlers with a structlog JSON formatter. All downstream `logging.getLogger(...)` calls automatically produce JSON.

---

## Correlation ID Propagation

A correlation ID is a UUID4 that links all log and trace events for a single pipeline pass or API request.

### Pipeline
`run_pipeline.run()` generates a fresh UUID4 at the start of each pass and calls `set_correlation_id()`.

### FastAPI
`CorrelationIDMiddleware` reads `X-Correlation-ID` from the incoming request header (or generates a new UUID if absent), sets the context var, and echoes the ID back in the response header.

```python
from config.correlation import set_correlation_id, get_correlation_id, mask_wallet
```

### Wallet Masking Policy

**No full Stellar wallet address (56 characters, `G…`) may appear in INFO-level or above log output.**

Use `mask_wallet(addr)` wherever a wallet address must be logged:

```python
from config.correlation import mask_wallet
mask_wallet("GABC1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234WXYZ")
# → "GABC1234...WXYZ"
```

The masking rule: first 8 characters + `...` + last 4 characters.

---

## OpenTelemetry Distributed Tracing

### Initialisation

```python
from config.telemetry import init_telemetry
init_telemetry("ledgerlens")
```

- If `OTEL_EXPORTER_OTLP_ENDPOINT` is set, spans are exported via OTLP gRPC.
- Otherwise, `ConsoleSpanExporter` is used (stdout).
- If the OTLP endpoint is unreachable, a `WARNING` is logged and the process continues without tracing.

### Instrumented Spans

| Span Name | Attributes | Location |
|---|---|---|
| `pipeline.run` | `pipeline.pair_count` | `run_pipeline.run()` |
| `model.score_batch` | `model.batch_size` | `detection/model_inference.py` |
| `soroban.submit_score` | `soroban.wallet` (masked), `soroban.score`, `soroban.dry_run` | `detection/soroban_publisher.py` |
| `webhook.deliver` | `webhook.subscriber_id`, `webhook.attempt` | `detection/webhook_worker.py` |

FastAPI routes are auto-instrumented via `opentelemetry-instrumentation-fastapi`.

### mTLS Configuration

To enable mTLS for the OTLP exporter, set all three of:

```bash
OTEL_EXPORTER_OTLP_CERTIFICATE=/path/to/ca.crt
OTEL_EXPORTER_OTLP_CLIENT_KEY=/path/to/client.key
OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE=/path/to/client.crt
```

### Trace Sampling

LedgerLens supports two sampling strategies:

1. **Static (head-based)** (default): Makes sampling decisions when spans start, using `OTEL_TRACES_SAMPLER`.
2. **Tail (tail-based)**: Makes sampling decisions after traces complete, based on trace characteristics.

#### Tail Sampling Policies

When `TRACE_SAMPLING_STRATEGY="tail"`, the following policies are applied:

| Policy | Description |
|---|---|
| Error | Always keep traces with any span in error state |
| Slow | Always keep traces with any span taking > 2000ms |
| Circuit Open | Always keep traces with `soroban.submit_score` spans where `circuit_state != closed` |
| Baseline | Keep 5% of remaining "boring" traces (configurable with `TRACE_TAIL_BASELINE_RATIO`) |

All kept traces have a `ledgerlens.sampling.reason` attribute indicating why they were kept (`error`, `slow`, `circuit_open`, or `baseline`).

#### Configuration

| Variable | Default | Description |
|---|---|---|
| `TRACE_SAMPLING_STRATEGY` | `static` | Sampling strategy: `static` or `tail` |
| `TRACE_TAIL_BASELINE_RATIO` | `0.05` | Fraction of "boring" traces to keep |
| `TRACE_TAIL_BUFFER_TIMEOUT_SECONDS` | `30.0` | Max time to wait for a trace to complete |
| `TRACE_TAIL_MAX_BUFFERED_TRACES` | `10000` | Max traces to buffer in memory |

#### Memory Management

- Traces are automatically flushed after `TRACE_TAIL_BUFFER_TIMEOUT_SECONDS`
- If the buffer hits `TRACE_TAIL_MAX_BUFFERED_TRACES`, the oldest trace is dropped
- The buffer runs in a background thread to avoid blocking application processing

---

## Prometheus Metrics

Exposed at `GET /metrics` (no auth — standard Prometheus scrape convention).

| Metric | Type | Labels | Description |
|---|---|---|---|
| `ledgerlens_wallets_scored_total` | Counter | `asset_pair`, `result` | Total wallets scored |
| `ledgerlens_scoring_latency_seconds` | Histogram | `asset_pair` | End-to-end wallet scoring time |
| `ledgerlens_soroban_submissions_total` | Counter | `status` | Total Soroban submissions |
| `ledgerlens_soroban_submission_latency_seconds` | Histogram | — | `submit_score()` call duration |
| `ledgerlens_circuit_breaker_open_total` | Counter | — | Circuit breaker open events |
| `ledgerlens_webhook_deliveries_total` | Counter | `result` | Webhook delivery attempts |
| `ledgerlens_drift_detected_total` | Counter | — | Feature drift detection events |
| `ledgerlens_pipeline_run_duration_seconds` | Histogram | — | Full pipeline pass duration |
| `ledgerlens_api_request_duration_seconds` | Histogram | `method`, `endpoint`, `status_code` | FastAPI request duration |
| `ledgerlens_model_auc_roc` | Gauge | `model_name` | Latest AUC-ROC per model |
| `ledgerlens_secret_rotation_total` | Counter | `secret_type`, `result` | Total secret rotation attempts |
| `ledgerlens_secret_rotation_overdue` | Gauge | — | Count of active keys exceeding maximum age without rotation |

**Security**: metric labels never contain wallet addresses, asset pair names beyond their label definition, or any PII.

---

## Alerting Rules (`monitoring/alerts.yml`)

### SorobanCircuitBreakerOpen

**Condition**: `increase(ledgerlens_circuit_breaker_open_total[5m]) > 0`

**Runbook**: The Soroban publisher has tripped its circuit breaker. Check `SOROBAN_RPC_URL` connectivity, verify `LEDGERLENS_SERVICE_SECRET_KEY` is correct and the service account is authorised to call `submit_score()`. The circuit auto-resets after `SOROBAN_CIRCUIT_RESET_SECONDS` (default 300s). Inspect logs for `SorobanCircuitOpenError`.

---

### WebhookDeadLetterBacklog

**Condition**: `increase(ledgerlens_webhook_deliveries_total{result="dead_lettered"}[1h]) > 10`

**Runbook**: More than 10 webhook deliveries permanently failed in the last hour. Inspect `GET /webhooks/dead-letters`. Verify subscriber URLs are reachable over HTTPS. Check that `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY` is set. Dead-letter items require manual intervention — delete the subscriber and re-register with a working URL if the endpoint is permanently unreachable.

---

### FeatureDriftDetected

**Condition**: `increase(ledgerlens_drift_detected_total[24h]) > 0`

**Runbook**: Feature distribution drift was recorded. Run `python cli.py retrain-check` to view the PSI report. If PSI > 0.25 on Benford or volume features, run `python cli.py retrain-check --force-retrain`. Check `GET /admin/drift-reports` for the full PSI breakdown.

---

### ScoringLatencyHigh

**Condition**: `histogram_quantile(0.95, rate(ledgerlens_scoring_latency_seconds_bucket[10m])) > 2.0` for 5 minutes

**Runbook**: p95 wallet scoring latency exceeds 2 seconds. Check Horizon API latency (`HORIZON_URL`), model inference load, and SQLite write throughput. Consider reducing `TRADE_HISTORY_LOOKBACK_DAYS` or running `async_run()` instead of synchronous `run()`. Retained alongside new SLO burn-rate rules as a coarse backstop symptom alert.

---

### PipelineStalled

**Condition**: `(time() - ledgerlens_pipeline_run_duration_seconds_created) > 300`

**Runbook**: The detection pipeline has not completed a run in over 5 minutes. Check that `python run_pipeline.py` (or `cli.py score`) is still running and not blocked. Review logs for exceptions in Horizon ingestion or model loading. Verify `LEDGERLENS_DB_PATH` is writable.

---

### SecretRotationOverdue

**Condition**: `ledgerlens_secret_rotation_overdue > 0`

**Runbook**: One or more active API keys have exceeded `API_KEY_MAX_AGE_DAYS` without rotation. Identify the overdue keys in the database and coordinate rotation via `POST /admin/api-keys/{key_id}/rotate` or the namespace key rotation endpoint.

### CapacityLimitApproaching

**Condition**: `ledgerlens:replica_count_projected_days_to_max < 14 or ledgerlens:pvc_projected_days_to_full < 14` for 1 hour

**Runbook**: Current growth trends indicate API replica count or PVC usage will hit configured limits within 14 days. Review `ledgerlens_wallets_scored_total` growth trend in the cost_capacity dashboard. Consider raising `autoscaling.maxReplicas` or `persistence.size` in `helm/ledgerlens/values.yaml`. Investigate unexpected ingestion volume growth with `GET /metrics` and Horizon SSE metrics. See [docs/cost_and_capacity.md](cost_and_capacity.md) for full runbook.

---

## Cost and Capacity Metrics

LedgerLens includes cost visibility and capacity projection metrics. See [docs/cost_and_capacity.md](cost_and_capacity.md) for configuration and the full Grafana dashboard.

**Key metrics:**

- `ledgerlens:pod_cost_per_hour:usd` — Cost per pod (CPU + memory)
- `ledgerlens:cost_per_wallet_scored:usd` — Unit economics (cost per scored wallet)
- `ledgerlens:replica_count_projected_days_to_max` — Days until maxReplicas exhausted
- `ledgerlens:pvc_projected_days_to_full` — Days until PVC full

**Recording rules:** `monitoring/recording_rules_cost.yml`  
**Alert:** `CapacityLimitApproaching` (fires 14 days before projected limit)

---

### SLO Burn-Rate Alerts

LedgerLens implements multi-window, multi-burn-rate alerting for the core user journeys (scoring latency, webhook delivery, Soroban submission, and score availability). Each journey has two alert rules:
1. **`<Journey>SLOFastBurn`**: Triggers when current consumption rate will exhaust the 30-day budget in under 2 days. Severity is `page` and routes to on-call operators.
2. **`<Journey>SLOSlowBurn`**: Triggers when current consumption rate will exhaust the 30-day budget in under 5 days. Severity is `ticket` and routes to Slack / ticketing systems.

See [docs/slo.md](file:///c:/Users/hp/drips/kosiso/Ledgerlens-core/docs/slo.md) for full metrics and alerting math details.

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | OTLP gRPC endpoint; falls back to console |
| `OTEL_EXPORTER_OTLP_CERTIFICATE` | *(unset)* | CA root cert for mTLS |
| `OTEL_EXPORTER_OTLP_CLIENT_KEY` | *(unset)* | Client private key for mTLS |
| `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` | *(unset)* | Client cert for mTLS |
| `SLO_SCORING_LATENCY_TARGET_SECONDS` | `2.0` | Target response latency threshold (seconds) |
| `SLO_SCORING_LATENCY_TARGET_PERCENT` | `99.0` | Target percentage of requests matching the latency threshold |
| `SLO_WEBHOOK_DELIVERY_TARGET_PERCENT` | `99.0` | Target success percentage for webhook delivery |
| `SLO_SOROBAN_SUBMISSION_TARGET_PERCENT` | `99.0` | Target success percentage for Soroban transaction submission |
| `SLO_WINDOW_DAYS` | `30` | Rolling window in days over which targets are measured |

See `.env.example` for all configuration variables.

---

## Model Cards

For model governance, compliance, and auditing, LedgerLens generates Model Cards for each promoted model version, including a Datasheet for the training dataset. For full details, see the [Model Cards documentation](./model_cards.md).
