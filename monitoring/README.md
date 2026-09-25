# LedgerLens Monitoring

This directory contains Prometheus alerting and recording rules, plus Grafana dashboards for LedgerLens observability.

## Contents

### Alert Rules

- **alerts.yml** — Production alerting rules for operational issues (circuit breakers, latency, drift detection, capacity limits)

All alerts include:
- Clear summary and description
- Runbook with remediation steps
- Appropriate `for` duration to reduce noise

### Recording Rules

- **recording_rules.yml** — SLO good/total ratios, error budgets, and the multi-window error-ratio inputs for burn-rate alerts
- **recording_rules_cost.yml** — Cost and capacity projection rules

These rules join Kubernetes resource metrics (kube-state-metrics, cadvisor) with configurable cost coefficients to produce:
- Per-pod cost metrics (CPU + memory)
- Cost per wallet scored
- Days-to-limit projections for replica count and PVC usage

### Grafana Dashboards

- **grafana/cost_capacity_dashboard.json** — Cost visibility and capacity planning dashboard
- **grafana/core_detection_dashboard.json** — Core detection throughput, API request latency (p50/p95/p99), and Horizon ingestion throughput. Panels are built on the metrics already referenced by `alerts.yml` and `recording_rules.yml` (`ledgerlens_api_request_duration_seconds`, `ledgerlens_scoring_latency_seconds`, `ledgerlens_wallets_scored_total`, `ledgerlens_pipeline_run_duration_seconds`, `ledgerlens_ingestion_events_*`). Auto-loads at `/d/ledgerlens-core-detection`.
- **grafana/provisioning/dashboards/ledgerlens.yaml** — Dashboard provisioning config

## Quick Start

### 1. Load Prometheus Rules

Add to your `prometheus.yml`:

```yaml
rule_files:
  - "/etc/prometheus/rules/alerts.yml"
  - "/etc/prometheus/rules/recording_rules_cost.yml"
```

Reload Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
# Or: killall -HUP prometheus
```

Verify rules are loaded:

```bash
curl -s http://localhost:9090/api/v1/rules | jq '.data.groups[] | select(.name == "ledgerlens_cost")'
```

### 2. Configure Cost Coefficients

Set cost values in `.env` (local) or Helm values (production):

```bash
# .env
COST_PER_VCPU_HOUR_USD=0.0416
COST_PER_GB_MEMORY_HOUR_USD=0.0056
COST_PER_GB_STORAGE_MONTH_USD=0.10
```

```yaml
# helm/ledgerlens/values.yaml
costConfig:
  enabled: true
  costPerVcpuHourUsd: "0.0416"
  costPerGbMemoryHourUsd: "0.0056"
  costPerGbStorageMonthUsd: "0.10"
```

Cost coefficients are exposed as Prometheus gauges at `GET /metrics` and referenced by recording rules.

### 3. Import Grafana Dashboard

**Option A: Manual import**

1. Grafana → Dashboards → Import
2. Upload `grafana/cost_capacity_dashboard.json`
3. Select Prometheus datasource

**Option B: Provisioned (recommended)**

```bash
# On Grafana server
sudo mkdir -p /var/lib/grafana/dashboards/ledgerlens
sudo cp grafana/cost_capacity_dashboard.json /var/lib/grafana/dashboards/ledgerlens/
sudo cp grafana/core_detection_dashboard.json /var/lib/grafana/dashboards/ledgerlens/
sudo cp grafana/provisioning/dashboards/ledgerlens.yaml /etc/grafana/provisioning/dashboards/
sudo systemctl restart grafana-server
```

The provisioning provider loads every `*.json` file in
`/var/lib/grafana/dashboards/ledgerlens`, so no config change is needed when
adding a dashboard — just drop the JSON in that directory.

Dashboards auto-load at `/d/ledgerlens-cost-capacity` and
`/d/ledgerlens-core-detection`.

## Validation

Validate rules before deploying:

```bash
# Install promtool (part of Prometheus distribution)
wget https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-amd64.tar.gz
tar xzf prometheus-2.45.0.linux-amd64.tar.gz
sudo mv prometheus-2.45.0.linux-amd64/promtool /usr/local/bin/

# Validate recording rules
promtool check rules monitoring/recording_rules_cost.yml

# Validate alerts
promtool check rules monitoring/alerts.yml

# Validate Grafana dashboard JSON
jq empty monitoring/grafana/cost_capacity_dashboard.json
jq empty monitoring/grafana/core_detection_dashboard.json
```

CI automatically validates rules on every PR (see `.github/workflows/cost-monitoring-validation.yml`).

## Alert Reference

### CapacityLimitApproaching

**Severity:** Warning  
**Condition:** `ledgerlens:replica_count_projected_days_to_max < 14 OR ledgerlens:pvc_projected_days_to_full < 14` for 1 hour

**What it means:** At current growth trends, LedgerLens will hit `autoscaling.maxReplicas` or fill the PVC within 14 days.

**Runbook:**
1. Open the cost & capacity dashboard
2. Check "Days Until Max Replicas" and "Days Until PVC Full" gauges
3. Review "Wallet Scoring Throughput" — is growth expected or anomalous?
4. If growth is expected: raise `autoscaling.maxReplicas` or `persistence.size` in Helm values
5. If growth is unexpected: investigate with `GET /metrics` and check for ingestion issues

See [docs/cost_and_capacity.md](../docs/cost_and_capacity.md) for the full runbook.

### SorobanCircuitBreakerOpen

**Severity:** Critical  
**Condition:** `increase(ledgerlens_circuit_breaker_open_total[5m]) > 0`

Circuit breaker tripped due to consecutive Soroban submission failures. Check:
- `SOROBAN_RPC_URL` connectivity
- `LEDGERLENS_SERVICE_SECRET_KEY` is correct
- Contract authorization for the service account

Circuit auto-resets after `SOROBAN_CIRCUIT_RESET_SECONDS` (default 300s).

### WebhookDeadLetterBacklog

**Severity:** Warning  
**Condition:** `increase(ledgerlens_webhook_deliveries_total{result="dead_lettered"}[1h]) > 10`

More than 10 webhook deliveries permanently failed in the last hour. Check:
- Subscriber URLs are reachable
- `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY` is set

Dead-letter items require manual intervention (view with `GET /webhooks/dead-letters`).

### FeatureDriftDetected

**Severity:** Warning  
**Condition:** `increase(ledgerlens_drift_detected_total[24h]) > 0`

Feature distribution drift detected. Run `python cli.py retrain-check` to view PSI report. If PSI > 0.25, consider retraining.

### ScoringLatencyHigh

**Severity:** Warning  
**Condition:** `histogram_quantile(0.95, rate(ledgerlens_scoring_latency_seconds_bucket[10m])) > 2.0` for 5 minutes

p95 wallet scoring latency exceeds 2 seconds. Check:
- Horizon API latency
- Model inference load
- SQLite write throughput

### PipelineStalled

**Severity:** Critical  
**Condition:** `(time() - ledgerlens_pipeline_run_duration_seconds_created) > 300`

Pipeline has not completed a run in over 5 minutes. Check:
- `python run_pipeline.py` is running
- No exceptions in logs
- `LEDGERLENS_DB_PATH` is writable

## SLO Burn-Rate Alerts

Each published SLO (defined in [docs/slo.md](../docs/slo.md)) has a fast-burn and a
slow-burn alert pair, following the Google SRE workbook multi-window,
multi-burn-rate design. Both windows must exceed the threshold, so a short blip
that has already recovered does not page, and the short window lets the alert
resolve quickly once the problem stops.

| SLO | Target | Budget | Fast burn (page) | Slow burn (ticket) |
|-----|--------|--------|------------------|--------------------|
| Scoring latency (<2s) | 99.0% | 1.0% | `ScoringLatencySLOFastBurn` | `ScoringLatencySLOSlowBurn` |
| Webhook delivery | 99.0% | 1.0% | `WebhookDeliverySLOFastBurn` | `WebhookDeliverySLOSlowBurn` |
| Soroban submission | 99.0% | 1.0% | `SorobanSubmissionSLOFastBurn` | `SorobanSubmissionSLOSlowBurn` |
| Score availability | 99.5% | 0.5% | `ScoreAvailabilitySLOFastBurn` | `ScoreAvailabilitySLOSlowBurn` |

| Alert class | Burn rate | Long / short window | `for` | Severity | Budget spent before firing |
|-------------|-----------|---------------------|-------|----------|----------------------------|
| Fast burn | 14.4x | 1h / 5m | 2m | `critical` | 2% of 30-day budget |
| Slow burn | 6x | 6h / 30m | 15m | `warning` | 5% of 30-day budget |

Inputs are the `ledgerlens:<slo>_slo:error_ratio_rate{5m,30m,1h,6h}` recording
rules in `recording_rules.yml`.

### Threshold validation

Thresholds follow the SRE workbook values, which are derived from error-budget
math rather than from any single incident. An alert can only fire after it has
consumed the budget fraction in the table above, so a transient 5-minute spike
(for example, a single Horizon outage blip) cannot trigger the fast-burn alert
unless it also pushes the 1h error ratio past 14.4x budget.

To measure the false-positive rate against historical data, backtest each alert
expression over the last 30 days of Prometheus data and compare firing intervals
with the incident log:

```bash
# Against a live Prometheus:
curl -G "$PROM/api/v1/query_range" \
  --data-urlencode 'query=ledgerlens:score_availability_slo:error_ratio_rate1h > (14.4 * 0.005) and ledgerlens:score_availability_slo:error_ratio_rate5m > (14.4 * 0.005)' \
  --data-urlencode "start=$(date -d '30 days ago' +%s)" --data-urlencode "end=$(date +%s)" --data-urlencode step=60
```

False-positive rate = firing episodes with no matching incident / total firing
episodes. Record the result here when thresholds are retuned; target is below 10%
for fast burn and below 25% for slow burn.

## Cardinality Guidelines

Unbounded label values (wallet addresses, transaction hashes, raw URLs, client
IPs) create a new time series per value and can take down the metrics backend.
`scripts/check_metric_cardinality.py` runs in CI and enforces
[`cardinality_budget.yml`](cardinality_budget.yml):

- **Forbidden labels** (`wallet`, `address`, `tx_hash`, `url`, `ip`, ...) are
  always rejected, in Python metric definitions and in `by (...)` clauses of any
  rule file in this directory.
- **Every label needs a declared bound.** Adding a new label means adding it to
  `labels:` with a maximum number of distinct values and a comment explaining
  what bounds it.
- **Per-metric series budget.** The product of a metric's label bounds (times
  bucket count for histograms) must stay under `max_series_per_metric` (5000).
  Exceeding it requires a `metric_overrides` entry with a written justification.

When adding a metric:

1. Label only by small, closed sets: status, result, method, route template,
   configured asset pair.
2. Never label by an identifier. Per-wallet detail belongs in logs, the audit
   trail, or the database, not Prometheus.
3. Normalise before labelling: route templates instead of paths, status class
   instead of arbitrary strings, bucketed values instead of raw numbers.
4. Run `python scripts/check_metric_cardinality.py` locally before opening a PR.

### Audit (current state)

No metric or recording rule uses a per-wallet or other unbounded label. The labels
with the largest value sets are bounded as follows:

| Label | Metrics | What bounds it |
|-------|---------|----------------|
| `asset_pair` | `wallets_scored_total`, `scoring_latency_seconds` | Only configured `ASSET_PAIRS` |
| `endpoint` | `api_request_duration_seconds`, `http_requests_total` | FastAPI route templates; `_normalise_endpoint()` for outbound calls |
| `namespace_id` | `waf_blocks_total` | One value per tenant namespace |
| `pod` | cost recording rules | Deployment replica count (churns on rollout; series expire) |

## Recording Rule Reference

### Cost Metrics

| Metric | Description | Unit |
|--------|-------------|------|
| `ledgerlens:pod_cost_per_hour:usd` | Per-pod cost (CPU + memory) | USD/hour |
| `ledgerlens:namespace_cost_per_hour:usd` | Total namespace cost | USD/hour |
| `ledgerlens:cost_per_wallet_scored:usd` | Unit cost per wallet scored | USD |
| `ledgerlens:storage_cost_per_hour:usd` | PVC cost (provisioned size) | USD/hour |

### Capacity Projection Metrics

| Metric | Description | Unit |
|--------|-------------|------|
| `ledgerlens:replica_count_projected_days_to_max` | Days until maxReplicas | days |
| `ledgerlens:pvc_projected_days_to_full` | Days until PVC full | days |
| `ledgerlens:wallets_scored_per_hour` | Current scoring rate | wallets/hour |
| `ledgerlens:wallets_scored_per_hour:predicted_7d` | Projected scoring rate (7d forward) | wallets/hour |

## Prerequisites

### Kubernetes Metrics

Cost and capacity rules require:

1. **kube-state-metrics** — for replica counts, PVC sizes, deployment status
2. **cadvisor** — for CPU/memory usage (included in kubelet)

Most Kubernetes distributions include these by default. If not:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
```

Verify metrics are scraped:

```bash
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job == "kube-state-metrics")'
```

### LedgerLens Metrics

Ensure Prometheus scrapes the LedgerLens `/metrics` endpoint:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'ledgerlens'
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: ['ledgerlens']
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
        action: keep
        regex: ledgerlens
      - source_labels: [__meta_kubernetes_pod_container_port_name]
        action: keep
        regex: http
```

## Troubleshooting

### Recording rules not appearing

1. Check rule file is loaded in `prometheus.yml`:
   ```yaml
   rule_files:
     - "monitoring/recording_rules_cost.yml"
   ```

2. Reload Prometheus:
   ```bash
   curl -X POST http://localhost:9090/-/reload
   ```

3. Check Prometheus logs:
   ```bash
   kubectl logs deployment/prometheus -n monitoring | grep recording_rules_cost
   ```

### Cost metrics are zero

1. Verify cost gauges are exposed:
   ```bash
   curl http://localhost:8000/metrics | grep ledgerlens_cost_per
   ```

2. Check `init_cost_metrics()` is called at startup (search logs for "Cost metrics initialized")

3. Verify kube-state-metrics are scraped:
   ```promql
   container_cpu_usage_seconds_total{namespace="ledgerlens"}
   ```

### Capacity projection is NaN or +Inf

1. **NaN:** Not enough data (< 7 days). Wait for data to accumulate.
2. **+Inf:** Zero or negative growth (not growing). Expected for stable workloads.
3. **Negative:** Usage is decreasing. Mathematically correct but not actionable.

### Dashboard shows "No data"

1. Verify Prometheus datasource is configured
2. Check recording rules are loaded (Prometheus → Status → Rules)
3. Verify `namespace="ledgerlens"` label matches your deployment

## Documentation

- [docs/cost_and_capacity.md](../docs/cost_and_capacity.md) — Full cost model, capacity projection methodology, dashboard guide
- [docs/observability.md](../docs/observability.md) — Structured logging, tracing, core metrics
- [docs/metrics.md](../docs/metrics.md) — Prometheus metrics catalogue
- [docs/kubernetes_deployment.md](../docs/kubernetes_deployment.md) — Helm chart deployment

## Security

### Cost Coefficient Confidentiality

Negotiated cloud discount rates are commercially sensitive. Do not commit actual production pricing to public repositories. Use:

- Default (on-demand) values in `values.yaml`
- `helm install --set` for production overrides
- Private values overlay (`values.prod.yaml` in secret manager)

### Dashboard Access Control

The cost dashboard exposes operational intelligence (replica counts, scoring rates). Enable Grafana authentication and RBAC to restrict access.

## CI/CD Integration

The `.github/workflows/cost-monitoring-validation.yml` workflow automatically validates:

- Prometheus rule syntax (with `promtool`)
- Grafana dashboard JSON schema
- Cost metrics unit tests

Runs on every PR touching monitoring files.
