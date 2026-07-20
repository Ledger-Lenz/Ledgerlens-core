# Cost and Capacity Monitoring Implementation Summary

This document summarizes the cost and capacity monitoring implementation for LedgerLens, delivered as part of the observability enhancement initiative.

## Overview

This implementation adds comprehensive cost visibility and capacity planning to LedgerLens's existing observability stack. It translates Kubernetes resource consumption into financial metrics and provides forward-looking capacity projections to answer:

- **What does LedgerLens cost to run?** (per hour, per wallet scored, per pod)
- **How much will it cost next month?** (trend analysis)
- **When will we run out of capacity?** (days until maxReplicas or PVC full)

## What Was Added

### Configuration

1. **`.env.example`** — Added 5 new environment variables for cost and capacity configuration:
   - `COST_PER_VCPU_HOUR_USD`
   - `COST_PER_GB_MEMORY_HOUR_USD`
   - `COST_PER_GB_STORAGE_MONTH_USD`
   - `CAPACITY_PROJECTION_WINDOW_DAYS`
   - `CAPACITY_PROJECTION_LEAD_TIME_DAYS`

2. **`config/settings.py`** — Added configuration fields with validation:
   - Cost coefficients must be non-negative
   - Capacity projection days must be >= 1
   - Follows existing pydantic-settings pattern

3. **`config/cost_exporter.py`** — NEW: Lightweight exporter that exposes cost coefficients as Prometheus gauges:
   - `ledgerlens_cost_per_vcpu_hour_usd`
   - `ledgerlens_cost_per_gb_memory_hour_usd`
   - `ledgerlens_cost_per_gb_storage_month_usd`
   - Initialized at application startup in `api/main.py`

### Prometheus Monitoring

1. **`monitoring/recording_rules_cost.yml`** — NEW: Recording rules for cost and capacity metrics:
   - **Cost metrics:**
     - `ledgerlens:pod_cost_per_hour:usd` — Per-pod cost (CPU + memory)
     - `ledgerlens:namespace_cost_per_hour:usd` — Total namespace cost
     - `ledgerlens:cost_per_wallet_scored:usd` — Unit economics
     - `ledgerlens:storage_cost_per_hour:usd` — PVC cost
   - **Capacity metrics:**
     - `ledgerlens:replica_count_projected_days_to_max` — Days until maxReplicas
     - `ledgerlens:pvc_projected_days_to_full` — Days until PVC full
     - `ledgerlens:wallets_scored_per_hour` — Current throughput
     - `ledgerlens:wallets_scored_per_hour:predicted_7d` — Projected throughput

2. **`monitoring/alerts.yml`** — Added `CapacityLimitApproaching` alert:
   - Fires when projected days-to-limit < 14 days
   - Comprehensive runbook with remediation steps
   - Follows existing alert pattern

3. **`monitoring/README.md`** — NEW: Comprehensive monitoring guide covering:
   - Alert reference with runbooks
   - Recording rule reference
   - Validation procedures
   - Troubleshooting guide
   - Security considerations

### Grafana Dashboard

1. **`monitoring/grafana/cost_capacity_dashboard.json`** — NEW: 8-panel dashboard:
   - Namespace Cost Per Hour (time series)
   - Cost Per Wallet Scored (gauge)
   - Cost Per Pod (stacked time series)
   - Days Until Max Replicas (gauge with thresholds)
   - Days Until PVC Full (gauge with thresholds)
   - Wallet Scoring Throughput (current vs projected)
   - API Replica Count (time series)
   - PVC Usage % (gauge)

2. **`monitoring/grafana/provisioning/dashboards/ledgerlens.yaml`** — NEW: Dashboard provisioning config for "dashboard-as-code" deployment

### Helm Chart

1. **`helm/ledgerlens/templates/cost-config.yaml`** — NEW: ConfigMap template for cost configuration

2. **`helm/ledgerlens/values.yaml`** — Added `costConfig` section:
   ```yaml
   costConfig:
     enabled: true
     costPerVcpuHourUsd: "0.0416"
     costPerGbMemoryHourUsd: "0.0056"
     costPerGbStorageMonthUsd: "0.10"
     capacityProjectionWindowDays: "7"
     capacityProjectionLeadTimeDays: "14"
   ```

3. **`helm/ledgerlens/templates/api-deployment.yaml`** — Updated to include cost-config ConfigMap in `envFrom`

### Documentation

1. **`docs/cost_and_capacity.md`** — NEW: 500+ line comprehensive guide covering:
   - Quick start (3 steps: configure, load rules, import dashboard)
   - Cost model explanation and coefficient configuration
   - Capacity projection methodology (predict_linear/deriv)
   - CapacityLimitApproaching alert runbook
   - Grafana dashboard panel descriptions
   - Prerequisites (kube-state-metrics, cadvisor)
   - Cost exporter implementation details
   - Helm deployment instructions
   - Limitations and caveats
   - Security considerations (cost coefficient confidentiality)
   - Troubleshooting guide

2. **`docs/observability.md`** — Updated to reference cost and capacity features

3. **`docs/metrics.md`** — Updated to reference cost recording rules

4. **`docs/kubernetes_deployment.md`** — Updated to document cost configuration in Helm chart

### Tests

1. **`tests/test_cost_metrics.py`** — NEW: Unit tests for cost exporter:
   - Test gauge initialization from settings
   - Test idempotency (repeated calls are no-ops)
   - Test metrics endpoint exposure
   - Test default values are reasonable
   - Test validation rejects negative coefficients
   - Test capacity projection configuration validation

### CI/CD

1. **`.github/workflows/cost-monitoring-validation.yml`** — NEW: GitHub Actions workflow that validates:
   - Prometheus recording rules syntax (with `promtool`)
   - Alert rules syntax
   - Grafana dashboard JSON schema
   - Required dashboard panels exist
   - Dashboard references correct metrics
   - Cost metrics unit tests pass

## How It Works

### Cost Model

1. **Configuration:** Operator sets cost coefficients in `.env` or Helm values (defaults to approximate on-demand pricing)

2. **Exporter:** At startup, `config/cost_exporter.py` sets three Prometheus gauges from `settings.py`

3. **Recording rules:** Prometheus multiplies kube-state-metrics/cadvisor resource usage by cost gauges to produce cost-per-pod, cost-per-namespace, and cost-per-wallet metrics

4. **Dashboard:** Grafana visualizes cost metrics over time

### Capacity Projection

1. **Recording rules:** Use Prometheus `deriv()` and `predict_linear()` to extrapolate current growth trends:
   - Compute growth rate over 7 days (configurable)
   - Calculate days remaining until maxReplicas or PVC full
   - Project future wallet scoring throughput

2. **Alert:** `CapacityLimitApproaching` fires when projected exhaustion is within 14 days (configurable)

3. **Dashboard:** Gauges display days-to-limit with color-coded thresholds (red < 7, orange 7-14, yellow 14-30, green > 30)

## Security Considerations

### Cost Coefficient Confidentiality

Negotiated cloud discount rates are commercially sensitive. The implementation:

- Uses default (on-demand) values in committed `values.yaml`
- Documents that production pricing should be overridden via `--set` or private values overlay
- Warns in comments not to commit actual negotiated rates to public repositories

### Dashboard Access Control

The cost dashboard exposes operational intelligence (replica counts, scoring rates). Documentation recommends:

- Enable Grafana authentication (disable anonymous access)
- Use Grafana RBAC to restrict dashboard access
- Do not expose Grafana publicly without authentication

### No PII in Metrics

Cost and capacity metrics aggregate resource consumption only. No wallet addresses, API keys, or transaction hashes appear in metric labels (consistent with existing metrics security policy).

## Prerequisites

The cost and capacity features require:

1. **kube-state-metrics** — for replica counts, PVC sizes, deployment status (included in most K8s distributions)

2. **cadvisor** — for CPU/memory usage (included in kubelet, no separate install needed)

3. **Prometheus** — scraping both LedgerLens `/metrics` endpoint and kube-state-metrics

4. **Grafana** — for dashboard visualization (optional but recommended)

## Testing

Validation is performed at multiple levels:

1. **Unit tests:** `tests/test_cost_metrics.py` verifies gauge initialization and configuration validation

2. **YAML validation:** CI validates Prometheus rules and Grafana dashboard JSON with `promtool` and `jq`

3. **Manual verification:** Documentation includes manual verification steps for staging:
   - Confirm cost metrics are exposed at `/metrics`
   - Verify recording rules appear in Prometheus
   - Check capacity projection produces sane values (non-Inf, non-NaN)
   - Test alert fires correctly against synthetic replica growth

## Deployment

### Local Development

```bash
# 1. Set cost coefficients in .env
echo "COST_PER_VCPU_HOUR_USD=0.0416" >> .env
echo "COST_PER_GB_MEMORY_HOUR_USD=0.0056" >> .env
echo "COST_PER_GB_STORAGE_MONTH_USD=0.10" >> .env

# 2. Start the API (cost metrics initialize at startup)
uvicorn api.main:app --reload

# 3. Verify cost gauges are exposed
curl http://localhost:8000/metrics | grep ledgerlens_cost_per
```

### Kubernetes / Helm

```bash
# 1. Update values.yaml with your cloud pricing
# Or use --set overrides for sensitive values

# 2. Install/upgrade
helm upgrade --install ledgerlens ./helm/ledgerlens \
  --set costConfig.costPerVcpuHourUsd=0.0416

# 3. Load Prometheus rules
kubectl apply -f monitoring/recording_rules_cost.yml
kubectl apply -f monitoring/alerts.yml

# 4. Import Grafana dashboard
# Upload monitoring/grafana/cost_capacity_dashboard.json via Grafana UI
# Or provision via monitoring/grafana/provisioning/dashboards/ledgerlens.yaml
```

## Files Changed/Added

### New Files (12)

- `config/cost_exporter.py`
- `monitoring/recording_rules_cost.yml`
- `monitoring/grafana/cost_capacity_dashboard.json`
- `monitoring/grafana/provisioning/dashboards/ledgerlens.yaml`
- `monitoring/README.md`
- `helm/ledgerlens/templates/cost-config.yaml`
- `docs/cost_and_capacity.md`
- `tests/test_cost_metrics.py`
- `.github/workflows/cost-monitoring-validation.yml`
- `COST_CAPACITY_IMPLEMENTATION.md` (this file)

### Modified Files (7)

- `.env.example` — Added 5 cost/capacity configuration variables
- `config/settings.py` — Added 5 configuration fields with validators
- `api/main.py` — Added `init_cost_metrics()` call at startup
- `helm/ledgerlens/values.yaml` — Added `costConfig` section
- `helm/ledgerlens/templates/api-deployment.yaml` — Added cost-config ConfigMap to envFrom
- `monitoring/alerts.yml` — Added `CapacityLimitApproaching` alert
- `docs/observability.md` — Added cost and capacity section
- `docs/metrics.md` — Added reference to cost recording rules
- `docs/kubernetes_deployment.md` — Added cost configuration documentation

## Definition of Done Checklist

- [x] All objectives completed:
  - [x] Cost coefficient configuration in `.env.example` and `config/settings.py`
  - [x] Lightweight cost exporter (`config/cost_exporter.py`)
  - [x] Recording rules (`monitoring/recording_rules_cost.yml`)
  - [x] Grafana dashboard (`monitoring/grafana/cost_capacity_dashboard.json`)
  - [x] Capacity projection recording rules using `predict_linear()`
  - [x] `CapacityLimitApproaching` alert added to `monitoring/alerts.yml`
  - [x] Dashboard provisioning snippet (`monitoring/grafana/provisioning/dashboards/ledgerlens.yaml`)
  - [x] Helm cost-config ConfigMap template

- [x] Tests written:
  - [x] Unit tests for cost exporter (`tests/test_cost_metrics.py`)
  - [x] CI validation workflow (`.github/workflows/cost-monitoring-validation.yml`)
  - [x] promtool validation for recording rules and alerts
  - [x] JSON schema validation for dashboard

- [x] Documentation complete:
  - [x] New `docs/cost_and_capacity.md` (comprehensive guide)
  - [x] Updated `docs/observability.md` with cost and capacity section
  - [x] Updated `docs/kubernetes_deployment.md` with cost config instructions
  - [x] Updated `.env.example` with 5 new variables
  - [x] Created `monitoring/README.md` with alert reference and troubleshooting

- [x] No regressions:
  - [x] Existing tests continue to pass (no changes to core detection logic)
  - [x] Cost exporter is optional (no impact if not initialized)
  - [x] Dashboard and recording rules are additive (don't modify existing metrics)

## Next Steps (Post-Implementation)

1. **Deploy to staging:** Test the full stack with real Kubernetes metrics

2. **Calibrate cost coefficients:** Replace defaults with actual cloud pricing

3. **Tune projection window:** Adjust `CAPACITY_PROJECTION_WINDOW_DAYS` based on traffic pattern stability

4. **Extend alerting:** Consider additional alerts for cost anomalies (e.g., cost-per-wallet > threshold)

5. **Add more panels:** Future dashboard enhancements could include:
   - Month-over-month cost comparison
   - Cost breakdown by component (API vs worker)
   - Budget tracking (actual vs planned spend)

## References

- [Issue #XXX] — Original issue requesting cost and capacity monitoring
- [docs/cost_and_capacity.md](docs/cost_and_capacity.md) — Comprehensive user guide
- [monitoring/README.md](monitoring/README.md) — Monitoring quick reference

---

**Implementation Date:** 2026-07-18  
**Author:** Kiro AI Agent  
**Status:** Complete, ready for review
