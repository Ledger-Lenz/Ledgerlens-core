# Cost and Capacity Planning

LedgerLens's cost and capacity monitoring stack translates Kubernetes resource consumption into financial visibility and forward-looking capacity projections, enabling operators to answer:

- **What does LedgerLens cost to run?** (per hour, per wallet scored, per pod)
- **How much will it cost next month?** (trend analysis)
- **When will we run out of capacity?** (days until maxReplicas or PVC full)

This document covers cost model configuration, recording rules, capacity projection methodology, and the Grafana dashboard.

---

## Quick Start

### 1. Configure Cost Coefficients

Set your actual cloud pricing in `.env` or Helm values:

```bash
# .env (local development)
COST_PER_VCPU_HOUR_USD=0.0416
COST_PER_GB_MEMORY_HOUR_USD=0.0056
COST_PER_GB_STORAGE_MONTH_USD=0.10
```

```yaml
# helm/ledgerlens/values.yaml (production)
costConfig:
  enabled: true
  costPerVcpuHourUsd: "0.0416"   # Replace with your negotiated rate
  costPerGbMemoryHourUsd: "0.0056"
  costPerGbStorageMonthUsd: "0.10"
```

**Security note:** Negotiated cloud discount rates are commercially sensitive. Do not commit actual production pricing to public repositories. Use `helm install --set` or a private values overlay.

### 2. Load Recording Rules

Add the cost recording rules to your Prometheus configuration:

```yaml
# prometheus.yml
rule_files:
  - "monitoring/alerts.yml"
  - "monitoring/recording_rules_cost.yml"
```

Reload Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
# Or: kubectl rollout restart deployment/prometheus -n monitoring
```

### 3. Import the Grafana Dashboard

**Option A: Manual import**

1. Open Grafana → Dashboards → Import
2. Upload `monitoring/grafana/cost_capacity_dashboard.json`
3. Select your Prometheus datasource

**Option B: Provisioned dashboard (recommended)**

Copy the provisioning config to your Grafana server:

```bash
# On the Grafana server
sudo mkdir -p /var/lib/grafana/dashboards/ledgerlens
sudo cp monitoring/grafana/cost_capacity_dashboard.json /var/lib/grafana/dashboards/ledgerlens/
sudo cp monitoring/grafana/provisioning/dashboards/ledgerlens.yaml /etc/grafana/provisioning/dashboards/

sudo systemctl restart grafana-server
```

The dashboard auto-loads at `http://your-grafana/d/ledgerlens-cost-capacity`.

---

## Cost Model

### Cost Coefficients

Three configurable coefficients translate resource usage into USD:

| Coefficient | Default | Unit | Description |
|-------------|---------|------|-------------|
| `COST_PER_VCPU_HOUR_USD` | `0.0416` | USD per vCPU-hour | Approximate on-demand vCPU cost (e.g., AWS c6i.large spot ≈ $0.0416/vCPU-hr) |
| `COST_PER_GB_MEMORY_HOUR_USD` | `0.0056` | USD per GB-hour | Approximate on-demand memory cost |
| `COST_PER_GB_STORAGE_MONTH_USD` | `0.10` | USD per GB-month | Block storage cost (e.g., AWS gp3 ≈ $0.08/GB-month) |

**How to determine your actual cost:**

1. **Compute (CPU + memory):** Check your cloud provider's pricing page for the instance type running your Kubernetes nodes. Divide the hourly instance cost by the number of vCPUs and GB of RAM to get per-unit rates.

2. **Storage:** Check the cost per GB-month for the storage class used by LedgerLens's PersistentVolumeClaim (typically gp3, pd-standard, or equivalent).

3. **Reserved instances / savings plans:** If you're running on reserved capacity, use the effective hourly rate (reservation cost / reservation hours) rather than on-demand pricing.

### Cost Recording Rules

The cost recording rules (in `monitoring/recording_rules_cost.yml`) aggregate kube-state-metrics and cadvisor metrics with the configured coefficients:

#### `ledgerlens:pod_cost_per_hour:usd`

Per-pod cost (CPU + memory) per hour:

```promql
(
  sum by (pod, namespace) (rate(container_cpu_usage_seconds_total{namespace="ledgerlens"}[5m]))
  * ledgerlens_cost_per_vcpu_hour_usd
)
+
(
  sum by (pod, namespace) (container_memory_working_set_bytes{namespace="ledgerlens"}) / 1073741824
  * ledgerlens_cost_per_gb_memory_hour_usd
)
```

**What it measures:**
- CPU: average CPU usage over 5 minutes (cores) × cost per vCPU-hour
- Memory: current working set (GB) × cost per GB-hour

**Why working set, not requested/limit?** You pay for actual consumption, not reservations. `container_memory_working_set_bytes` is the memory actively in use (RSS + cache), which drives cloud billing.

#### `ledgerlens:namespace_cost_per_hour:usd`

Total cost across all LedgerLens pods:

```promql
sum(ledgerlens:pod_cost_per_hour:usd{namespace="ledgerlens"})
```

#### `ledgerlens:cost_per_wallet_scored:usd`

Cost per wallet scored (amortized over 1 hour):

```promql
sum(ledgerlens:pod_cost_per_hour:usd{namespace="ledgerlens"})
/
(sum(rate(ledgerlens_wallets_scored_total[1h])) * 3600)
```

**Interpretation:** If this value is `0.0001`, each scored wallet costs $0.0001 = $0.10 per 1,000 wallets.

**When it's NaN:** If no wallets are scored in the last hour (cold start, maintenance window), the denominator is zero and the result is NaN. The Grafana gauge panel displays "No data" in this case.

#### `ledgerlens:storage_cost_per_hour:usd`

Storage cost (PVC size × monthly cost / hours per month):

```promql
sum by (persistentvolumeclaim, namespace) (
  kube_persistentvolumeclaim_resource_requests_storage_bytes{namespace="ledgerlens"}
) / 1073741824
* ledgerlens_cost_per_gb_storage_month_usd
/ 730
```

**Note:** This computes cost based on *provisioned* PVC size, not actual usage. Cloud providers bill for the entire volume, not just the bytes written.

---

## Capacity Projection

Capacity projection uses Prometheus's `predict_linear()` and `deriv()` functions to extrapolate current growth trends and estimate when configured limits will be reached.

### Projection Window

The projection window (default: 7 days) controls how much historical data is used for the linear regression:

```bash
CAPACITY_PROJECTION_WINDOW_DAYS=7
```

- **Shorter window (3-5 days):** More responsive to recent changes, but noisier. Use when traffic patterns change frequently.
- **Longer window (14-30 days):** Smoother projection, less sensitive to short-term spikes. Use for stable, predictable workloads.

### Recording Rules

#### `ledgerlens:replica_count_projected_days_to_max`

Days until API replica count hits `autoscaling.maxReplicas`:

```promql
(10 - kube_deployment_status_replicas{deployment="ledgerlens-api", namespace="ledgerlens"})
/
clamp_min(deriv(kube_deployment_status_replicas{deployment="ledgerlens-api", namespace="ledgerlens"}[7d]) * 86400, 1e-9)
```

**How it works:**

1. **Numerator:** Remaining capacity (`maxReplicas - current`)
2. **Denominator:** Growth rate (replicas per day), computed as:
   - `deriv(...[7d])` = replicas per second over 7 days
   - `× 86400` = convert to replicas per day
   - `clamp_min(..., 1e-9)` = prevent division by zero (returns a very large number if growth is zero or negative)

**Interpretation:**

- `< 7 days`: Critical — scale up immediately or investigate unexpected growth
- `7-14 days`: Warning — plan capacity expansion
- `> 30 days`: Healthy
- `> 1000 days` or `+Inf`: No growth detected; capacity is not a concern

**When the projection is wrong:**

- **Step changes:** If traffic suddenly doubles (product launch, marketing campaign), the 7-day window underestimates growth. Review the dashboard after major events and manually adjust if needed.
- **Seasonal patterns:** Linear regression cannot capture weekly or monthly cycles. Use a longer window (30 days) to average out cycles, or supplement with external forecasting tools.

#### `ledgerlens:pvc_projected_days_to_full`

Days until PVC usage hits `persistence.size`:

```promql
(
  kubelet_volume_stats_capacity_bytes{namespace="ledgerlens", persistentvolumeclaim=~"ledgerlens-.*"}
  -
  kubelet_volume_stats_used_bytes{namespace="ledgerlens", persistentvolumeclaim=~"ledgerlens-.*"}
)
/
clamp_min(
  deriv(kubelet_volume_stats_used_bytes{namespace="ledgerlens", persistentvolumeclaim=~"ledgerlens-.*"}[7d]) * 86400,
  1e-9
)
```

**Same logic as replica projection**, but for storage bytes instead of replica count.

**Actionable threshold:** The `CapacityLimitApproaching` alert fires when this value drops below 14 days.

#### `ledgerlens:wallets_scored_per_hour`

Current scoring throughput (for capacity planning dashboards):

```promql
rate(ledgerlens_wallets_scored_total[1h]) * 3600
```

#### `ledgerlens:wallets_scored_per_hour:predicted_7d`

Projected scoring throughput 7 days from now:

```promql
predict_linear(ledgerlens_wallets_scored_total[7d], 7*86400)
- ledgerlens_wallets_scored_total
```

**Interpretation:** If current rate is 1000 wallets/hour and predicted is 1500, throughput is growing by 50% over the next week.

---

## CapacityLimitApproaching Alert

The `CapacityLimitApproaching` alert (in `monitoring/alerts.yml`) fires when projected capacity exhaustion is within the configured lead time:

```yaml
- alert: CapacityLimitApproaching
  expr: |
    ledgerlens:replica_count_projected_days_to_max < 14
    or
    ledgerlens:pvc_projected_days_to_full < 14
  for: 1h
  annotations:
    summary: "Cluster capacity projected to be exhausted within 14 days"
    runbook: "Review ledgerlens_wallets_scored_total growth trend..."
```

**Lead time configuration:**

```bash
CAPACITY_PROJECTION_LEAD_TIME_DAYS=14
```

- **Shorter lead time (7 days):** Fewer false positives, but less time to react. Use when you can provision new capacity quickly (e.g., auto-scaling cloud environment).
- **Longer lead time (30 days):** More advance warning, but may trigger on temporary spikes. Use when procurement cycles are slow (e.g., on-premises hardware).

### Runbook

When the alert fires:

1. **Check the dashboard:** Open the cost & capacity dashboard and review:
   - "Days Until Max Replicas" gauge
   - "Days Until PVC Full" gauge
   - "Wallet Scoring Throughput" trend (is growth expected or anomalous?)

2. **Investigate growth cause:**
   - Check `GET /metrics` for `ledgerlens_wallets_scored_total` by `asset_pair` — has a specific market driven the growth?
   - Review Horizon SSE metrics — is ingestion volume up?
   - Check for data quality issues — are duplicate trades being ingested?

3. **Take action:**
   - **Replica limit:** Increase `autoscaling.maxReplicas` in `helm/ledgerlens/values.yaml` and `helm upgrade`
   - **PVC full:** Expand the PVC (if your storage class supports `allowVolumeExpansion: true`) or migrate to a larger volume
   - **Cost concern:** If growth is expected but budget-constrained, tune `TRADE_HISTORY_LOOKBACK_DAYS` or filter low-value asset pairs

4. **Silence the alert (if growth is expected):**
   ```bash
   # If mainnet launch traffic is expected, silence for 7 days
   amtool silence add --comment="Expected mainnet launch traffic" \
     alertname=CapacityLimitApproaching --duration=7d
   ```

---

## Grafana Dashboard

The cost & capacity dashboard (`monitoring/grafana/cost_capacity_dashboard.json`) provides 8 panels:

### 1. Namespace Cost Per Hour

**Type:** Time series  
**Metric:** `ledgerlens:namespace_cost_per_hour:usd`

Total cost across all LedgerLens pods. Useful for:
- Daily cost estimation (cost per hour × 24)
- Detecting cost spikes (e.g., unexpected autoscaling)

### 2. Cost Per Wallet Scored

**Type:** Gauge  
**Metric:** `ledgerlens:cost_per_wallet_scored:usd`

Unit economics: how much does it cost to score one wallet? Lower is better.

**Thresholds:**
- Green: < $0.001 (< $1 per 1,000 wallets)
- Yellow: $0.001 - $0.01
- Red: > $0.01 (> $10 per 1,000 wallets)

### 3. Cost Per Pod (Stacked)

**Type:** Time series (stacked area)  
**Metric:** `ledgerlens:pod_cost_per_hour:usd`

Per-pod cost breakdown. Useful for:
- Identifying expensive pods (API vs ingestion worker)
- Spotting resource leaks (gradual cost increase in a single pod)

### 4. Days Until Max Replicas

**Type:** Gauge  
**Metric:** `ledgerlens:replica_count_projected_days_to_max`

**Thresholds:**
- Red: < 7 days (critical — act now)
- Orange: 7-14 days (warning — plan expansion)
- Yellow: 14-30 days (monitor)
- Green: > 30 days (healthy)

### 5. Days Until PVC Full

**Type:** Gauge  
**Metric:** `ledgerlens:pvc_projected_days_to_full`

Same thresholds as replica projection.

### 6. Wallet Scoring Throughput (Current vs Projected)

**Type:** Time series  
**Metrics:**
- `ledgerlens:wallets_scored_per_hour` (solid line)
- `ledgerlens:wallets_scored_per_hour:predicted_7d / (7*86400) * 3600` (dashed line)

Shows current and projected scoring rate. Helps answer "will we be able to handle next week's traffic?"

### 7. API Replica Count

**Type:** Time series  
**Metric:** `kube_deployment_status_replicas{deployment="ledgerlens-api"}`

Current replica count over time. The red threshold line at `maxReplicas=10` indicates the hard limit.

### 8. PVC Usage %

**Type:** Gauge  
**Metric:** `(kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes) * 100`

Current storage utilization percentage.

**Thresholds:**
- Green: < 70%
- Yellow: 70-85%
- Red: > 85%

---

## Prerequisites

### Kubernetes Metrics

The cost and capacity recording rules require standard Kubernetes metrics from:

1. **kube-state-metrics** (for replica counts, PVC sizes)
2. **cadvisor** (for CPU/memory usage, included in kubelet)

Most Kubernetes distributions (EKS, GKE, AKS, k3s) include these by default. If not:

```bash
# Install kube-state-metrics via Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
```

Verify metrics are scraped:

```bash
# Check Prometheus targets
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job == "kube-state-metrics")'
```

### Prometheus Scrape Config

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

---

## Cost Exporter Implementation

Cost coefficients are exposed as Prometheus gauges via `config/cost_exporter.py`. The exporter is initialized at application startup:

```python
# api/main.py
from config.cost_exporter import init_cost_metrics

@app.on_event("startup")
async def startup():
    init_cost_metrics()  # Sets cost coefficient gauges from settings.py
    # ... other startup tasks
```

The three gauges are:

- `ledgerlens_cost_per_vcpu_hour_usd`
- `ledgerlens_cost_per_gb_memory_hour_usd`
- `ledgerlens_cost_per_gb_storage_month_usd`

They are static values set once at startup. If you change the cost coefficients in `.env` or Helm values, restart the application for the new values to take effect.

---

## Deployment with Helm

The Helm chart includes a `costConfig` section in `values.yaml`:

```yaml
costConfig:
  enabled: true
  costPerVcpuHourUsd: "0.0416"
  costPerGbMemoryHourUsd: "0.0056"
  costPerGbStorageMonthUsd: "0.10"
  capacityProjectionWindowDays: "7"
  capacityProjectionLeadTimeDays: "14"
```

These values are injected into the API deployment via a ConfigMap:

```bash
helm install ledgerlens ./helm/ledgerlens \
  --set costConfig.costPerVcpuHourUsd=0.05 \
  --set costConfig.costPerGbMemoryHourUsd=0.007
```

The ConfigMap is automatically mounted as environment variables in the API pods.

---

## Limitations and Caveats

### 1. Linear Projection Assumptions

The capacity projection rules assume **linear growth**. They cannot predict:

- Step changes (product launches, marketing campaigns)
- Seasonal patterns (weekly or monthly cycles)
- Exponential growth (viral adoption)

**Mitigation:** Review the dashboard after major events and manually adjust projections if needed. Consider using external forecasting tools (e.g., Prophet, ARIMA) for non-linear patterns.

### 2. Cost Model Simplifications

The cost model assumes:

- Homogeneous instance types (all nodes have the same $/vCPU and $/GB rates)
- On-demand pricing (no reserved instances, spot instances, or savings plans)
- No egress/ingress costs (cloud providers charge for data transfer, which is not modeled here)

**Mitigation:** Adjust cost coefficients to reflect your actual blended rate (total monthly cost / total vCPU-hours and GB-hours). For heterogeneous clusters, use node labels and separate recording rules per instance type.

### 3. Cold Start / No Data

If Prometheus has less than 7 days of historical data (new deployment, recent Prometheus restart), the `deriv(...[7d])` queries return no data and the capacity projections are unavailable.

**Mitigation:** The dashboard displays "No data" in this case. Wait until 7 days of data accumulates, or reduce `CAPACITY_PROJECTION_WINDOW_DAYS` to 3 days for faster bootstrapping.

### 4. PVC Expansion Limitations

Some storage classes do not support `allowVolumeExpansion: true`. In this case, expanding a PVC requires creating a new PVC and migrating data.

**Mitigation:** Check your storage class manifest before deploying:

```bash
kubectl get storageclass gp3 -o yaml | grep allowVolumeExpansion
```

If `false`, plan for PVC migration lead time (typically 1-2 days for large volumes).

---

## Security Considerations

### Cost Coefficient Confidentiality

Negotiated cloud discount rates are **commercially sensitive**. Publishing your actual cost coefficients in a public repository may:

- Leak information about your negotiated pricing to competitors
- Violate confidentiality clauses in your cloud contract

**Recommendation:**

1. Use default (on-demand) values in `values.yaml` committed to version control
2. Override with actual values at deployment time via `--set` or a private values overlay:

```bash
# values.prod.yaml (NOT committed to public repo)
costConfig:
  costPerVcpuHourUsd: "0.032"  # Your actual negotiated rate

helm install ledgerlens ./helm/ledgerlens -f values.prod.yaml
```

3. Store `values.prod.yaml` in a secret manager (Vault, AWS Secrets Manager) or private repo

### Dashboard Access Control

The cost dashboard exposes operational intelligence (replica counts, scoring rates) that should not be visible to unauthenticated users.

**Recommendation:**

1. Enable Grafana authentication (disable anonymous access)
2. Use Grafana's RBAC to restrict dashboard access to operators/SRE team only
3. Do not expose Grafana publicly without authentication

---

## Testing

Validate the recording rules and alert before deploying to production:

```bash
# Validate recording rules syntax
promtool check rules monitoring/recording_rules_cost.yml

# Validate alerts syntax
promtool check rules monitoring/alerts.yml

# Validate dashboard JSON schema
jq empty monitoring/grafana/cost_capacity_dashboard.json
```

Unit tests for the cost exporter are in `tests/test_cost_metrics.py`.

---

## Troubleshooting

### Recording rules not appearing in Prometheus

1. Check rule file is listed in `prometheus.yml`:
   ```yaml
   rule_files:
     - "monitoring/recording_rules_cost.yml"
   ```

2. Reload Prometheus:
   ```bash
   curl -X POST http://localhost:9090/-/reload
   ```

3. Check Prometheus logs for syntax errors:
   ```bash
   kubectl logs deployment/prometheus -n monitoring | grep recording_rules_cost
   ```

### Cost metrics are zero or missing

1. Verify cost coefficient gauges are set:
   ```bash
   curl http://localhost:8000/metrics | grep ledgerlens_cost_per
   ```

2. Check that `init_cost_metrics()` is called at startup (search for "Cost metrics initialized" in logs)

3. Verify kube-state-metrics and cadvisor metrics are scraped by Prometheus:
   ```promql
   container_cpu_usage_seconds_total{namespace="ledgerlens"}
   kube_deployment_status_replicas{deployment="ledgerlens-api"}
   ```

### Capacity projection is NaN or +Inf

1. **NaN:** Not enough data points for linear regression. Wait until 7 days of data accumulates.

2. **+Inf:** Growth rate is zero or negative (no growth detected). This is expected for stable workloads.

3. **Negative value:** Current usage is decreasing. The projection is mathematically correct but not actionable (you'll never hit the limit if usage is shrinking).

### Dashboard shows "No data"

1. Verify the Prometheus datasource is configured in Grafana
2. Check that recording rules are loaded (Prometheus → Status → Rules)
3. Verify the `namespace="ledgerlens"` label matches your actual namespace

---

## Related Documentation

- [docs/observability.md](observability.md) — Structured logging, tracing, and core metrics
- [docs/metrics.md](metrics.md) — Full Prometheus metrics catalogue
- [docs/kubernetes_deployment.md](kubernetes_deployment.md) — Helm chart configuration
- [docs/performance.md](performance.md) — Benchmark results and scale targets
- [docs/performance_monitoring.md](performance_monitoring.md) — Model accuracy degradation monitoring

---

## Changelog

- **2026-07-18:** Initial release (cost and capacity monitoring stack)
