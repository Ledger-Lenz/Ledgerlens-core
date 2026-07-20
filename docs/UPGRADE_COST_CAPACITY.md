# Upgrading to Cost and Capacity Monitoring

This guide walks you through enabling cost visibility and capacity planning for an existing LedgerLens deployment.

## Prerequisites Check

Before upgrading, verify your environment has the required metrics:

```bash
# 1. Check kube-state-metrics is installed
kubectl get pods -n monitoring | grep kube-state-metrics

# 2. Verify it's scraped by Prometheus
curl -s http://localhost:9090/api/v1/targets | \
  jq '.data.activeTargets[] | select(.labels.job == "kube-state-metrics")'

# 3. Check LedgerLens metrics are scraped
curl http://localhost:8000/metrics | grep ledgerlens_wallets_scored_total
```

If kube-state-metrics is missing:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
```

## Step 1: Update Configuration

### Option A: Local / Development

Add to your `.env`:

```bash
# Cost coefficients (adjust to your cloud pricing)
COST_PER_VCPU_HOUR_USD=0.0416
COST_PER_GB_MEMORY_HOUR_USD=0.0056
COST_PER_GB_STORAGE_MONTH_USD=0.10

# Capacity projection settings
CAPACITY_PROJECTION_WINDOW_DAYS=7
CAPACITY_PROJECTION_LEAD_TIME_DAYS=14
```

Restart the API:

```bash
# The API will log: "Cost metrics initialized: vCPU=$0.0416/hr, ..."
uvicorn api.main:app --reload
```

### Option B: Kubernetes / Helm

Update your Helm values or use `--set` overrides:

```yaml
# values.yaml (or values.prod.yaml)
costConfig:
  enabled: true
  costPerVcpuHourUsd: "0.0416"   # Replace with your actual rate
  costPerGbMemoryHourUsd: "0.0056"
  costPerGbStorageMonthUsd: "0.10"
  capacityProjectionWindowDays: "7"
  capacityProjectionLeadTimeDays: "14"
```

Upgrade the release:

```bash
helm upgrade ledgerlens ./helm/ledgerlens \
  -f values.prod.yaml \
  --namespace ledgerlens
```

**Important:** The upgrade will:
- Create a new ConfigMap (`ledgerlens-cost-config`)
- Update the API deployment to mount the ConfigMap
- Trigger a rolling restart of API pods

Wait for pods to be ready:

```bash
kubectl rollout status deployment/ledgerlens-api -n ledgerlens
```

## Step 2: Load Prometheus Rules

Copy recording rules and alerts to your Prometheus server:

```bash
# On the Prometheus server
sudo mkdir -p /etc/prometheus/rules
sudo cp monitoring/recording_rules_cost.yml /etc/prometheus/rules/
sudo cp monitoring/alerts.yml /etc/prometheus/rules/  # Updated with CapacityLimitApproaching
```

Update `prometheus.yml` to include the new rules:

```yaml
rule_files:
  - "/etc/prometheus/rules/alerts.yml"
  - "/etc/prometheus/rules/recording_rules_cost.yml"
```

Reload Prometheus:

```bash
# Send SIGHUP to reload config without restarting
curl -X POST http://localhost:9090/-/reload

# Or restart the Prometheus pod
kubectl rollout restart deployment/prometheus -n monitoring
```

Verify rules are loaded:

```bash
# Check recording rules appear
curl -s http://localhost:9090/api/v1/rules | \
  jq '.data.groups[] | select(.name == "ledgerlens_cost")'

# Check CapacityLimitApproaching alert is loaded
curl -s http://localhost:9090/api/v1/rules | \
  jq '.data.groups[].rules[] | select(.name == "CapacityLimitApproaching")'
```

## Step 3: Verify Cost Metrics

After the API restart, verify cost coefficient gauges are exposed:

```bash
# Check /metrics endpoint
curl http://localhost:8000/metrics | grep ledgerlens_cost_per

# Expected output:
# ledgerlens_cost_per_vcpu_hour_usd 0.0416
# ledgerlens_cost_per_gb_memory_hour_usd 0.0056
# ledgerlens_cost_per_gb_storage_month_usd 0.1
```

After 5 minutes (the recording rule interval), verify cost recording rules are evaluating:

```bash
# Check cost-per-pod metric
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:pod_cost_per_hour:usd' | jq .

# Check cost-per-wallet metric
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:cost_per_wallet_scored:usd' | jq .
```

If metrics are missing:
1. Check Prometheus logs for rule evaluation errors
2. Verify kube-state-metrics and cadvisor metrics are available
3. Verify the `namespace="ledgerlens"` label matches your deployment

## Step 4: Verify Capacity Projection

After 15 minutes (the capacity projection rule interval), check the projection metrics:

```bash
# Check days-to-max-replicas
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:replica_count_projected_days_to_max' | jq .

# Check days-to-full-PVC
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:pvc_projected_days_to_full' | jq .
```

**Expected behavior:**

- If you have < 7 days of historical data, the projection will be empty (insufficient data)
- If replica count is stable (not growing), the projection will be `+Inf` (never hit the limit)
- If growing, you'll see a positive number (days until limit)

## Step 5: Import Grafana Dashboard

### Option A: Manual Import

1. Open Grafana → Dashboards → Import
2. Click "Upload JSON file"
3. Select `monitoring/grafana/cost_capacity_dashboard.json`
4. Choose your Prometheus datasource
5. Click "Import"

The dashboard will be available at: `http://your-grafana/d/ledgerlens-cost-capacity`

### Option B: Provisioned Dashboard (Recommended)

On your Grafana server:

```bash
# Copy dashboard JSON
sudo mkdir -p /var/lib/grafana/dashboards/ledgerlens
sudo cp monitoring/grafana/cost_capacity_dashboard.json \
  /var/lib/grafana/dashboards/ledgerlens/

# Copy provisioning config
sudo cp monitoring/grafana/provisioning/dashboards/ledgerlens.yaml \
  /etc/grafana/provisioning/dashboards/

# Restart Grafana
sudo systemctl restart grafana-server

# Or for Kubernetes:
kubectl rollout restart deployment/grafana -n monitoring
```

The dashboard will auto-load on startup. No manual import needed.

## Step 6: Test the Alert

Test the `CapacityLimitApproaching` alert by triggering it manually:

```bash
# Open Alertmanager UI
open http://localhost:9093

# Or check Prometheus alerts
curl -s http://localhost:9090/api/v1/alerts | \
  jq '.data.alerts[] | select(.labels.alertname == "CapacityLimitApproaching")'
```

To **simulate** the alert without waiting for real capacity exhaustion:

```bash
# Temporarily lower the replica limit to trigger the alert
kubectl scale deployment/ledgerlens-api --replicas=9 -n ledgerlens

# Wait 1 hour (the alert's `for` duration)
# Check if CapacityLimitApproaching fires
```

**Important:** Scale back after testing:

```bash
kubectl scale deployment/ledgerlens-api --replicas=2 -n ledgerlens
```

## Step 7: Update Alerting Routes (Optional)

If you have Alertmanager configured, add a route for capacity alerts:

```yaml
# alertmanager.yml
route:
  receiver: 'default'
  routes:
    # Route capacity alerts to the SRE/ops team
    - match:
        alertname: CapacityLimitApproaching
      receiver: 'sre-team'
      continue: false

receivers:
  - name: 'sre-team'
    slack_configs:
      - api_url: 'YOUR_SLACK_WEBHOOK'
        channel: '#sre-alerts'
        title: 'LedgerLens Capacity Alert'
```

## Rollback Procedure

If you need to rollback:

### Step 1: Remove Prometheus Rules

```bash
# Comment out or remove the new rule file from prometheus.yml
# rule_files:
#   - "/etc/prometheus/rules/recording_rules_cost.yml"

curl -X POST http://localhost:9090/-/reload
```

### Step 2: Rollback Helm Release

```bash
# List release history
helm history ledgerlens -n ledgerlens

# Rollback to previous revision
helm rollback ledgerlens 1 -n ledgerlens
```

### Step 3: Remove Dashboard

In Grafana:
1. Navigate to the Cost & Capacity dashboard
2. Click Settings (gear icon)
3. Click "Delete dashboard"

Or for provisioned dashboards:

```bash
sudo rm /var/lib/grafana/dashboards/ledgerlens/cost_capacity_dashboard.json
sudo systemctl restart grafana-server
```

## Troubleshooting

### Cost Metrics Are Zero

**Symptom:** `ledgerlens:pod_cost_per_hour:usd` returns 0 or no data

**Causes:**

1. Cost coefficient gauges not initialized
   ```bash
   # Check API logs for "Cost metrics initialized"
   kubectl logs deployment/ledgerlens-api -n ledgerlens | grep "Cost metrics initialized"
   ```

2. kube-state-metrics/cadvisor not scraped
   ```bash
   # Check Prometheus targets
   curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job == "kubelet")'
   ```

3. Recording rule syntax error
   ```bash
   # Validate rules locally
   promtool check rules monitoring/recording_rules_cost.yml
   ```

**Fix:**

- Restart the API if gauges aren't set
- Verify Prometheus scrape config includes kubelet/kube-state-metrics
- Check Prometheus logs for rule evaluation errors

### Capacity Projection Is NaN or +Inf

**Symptom:** Dashboard shows "No data" for days-to-limit gauges

**Causes:**

1. **NaN:** Not enough historical data (< 7 days)
   - **Fix:** Wait until 7 days of replica count data accumulates, or reduce `CAPACITY_PROJECTION_WINDOW_DAYS` to 3

2. **+Inf:** Zero or negative growth (stable workload)
   - **Fix:** This is expected. If replica count is not growing, you'll never hit the limit. The gauge will show "No limit"

3. **Negative value:** Usage is decreasing
   - **Fix:** Mathematically correct but not actionable. The projection is saying "you're shrinking, so you'll never hit the limit"

### Alert Not Firing

**Symptom:** `CapacityLimitApproaching` doesn't fire despite low days-to-limit

**Causes:**

1. Alert `for` duration not elapsed (requires 1 hour of sustained breach)
   ```bash
   # Check pending alerts
   curl -s http://localhost:9090/api/v1/alerts | jq '.data.alerts[] | select(.labels.alertname == "CapacityLimitApproaching")'
   ```

2. Alert expression doesn't match your deployment
   - Verify `deployment="ledgerlens-api"` and `namespace="ledgerlens"` labels match your actual deployment

**Fix:**

- Wait 1 hour if the alert just started pending
- Adjust label selectors in `monitoring/alerts.yml` to match your deployment names

### Dashboard Shows Different Namespace

**Symptom:** All panels show "No data"

**Cause:** Dashboard hardcodes `namespace="ledgerlens"`

**Fix:** Edit the dashboard JSON and replace all occurrences of `namespace="ledgerlens"` with your actual namespace, or add a template variable:

1. Dashboard Settings → Variables → Add variable
2. Name: `namespace`
3. Query: `label_values(kube_deployment_status_replicas, namespace)`
4. Update all queries to use `namespace="$namespace"`

## Monitoring the Upgrade

Watch for these log messages after the upgrade:

```bash
# API startup — cost metrics initialized
kubectl logs deployment/ledgerlens-api -n ledgerlens | grep "Cost metrics initialized"
# Expected: Cost metrics initialized: vCPU=$0.0416/hr, Memory=$0.0056/GB-hr, Storage=$0.10/GB-month

# Prometheus — rules loaded
kubectl logs deployment/prometheus -n monitoring | grep recording_rules_cost
# Expected: Loaded 2 rule groups from /etc/prometheus/rules/recording_rules_cost.yml

# Grafana — dashboard provisioned
kubectl logs deployment/grafana -n monitoring | grep cost_capacity
# Expected: Provisioned dashboard: Cost & Capacity
```

## Post-Upgrade Checklist

After the upgrade, verify:

- [ ] Cost coefficient gauges are exposed at `/metrics`
- [ ] Recording rules are loaded in Prometheus
- [ ] Cost metrics are evaluating (non-zero values)
- [ ] Capacity projection metrics are present (may be +Inf if no growth)
- [ ] `CapacityLimitApproaching` alert is defined (check Prometheus alerts page)
- [ ] Grafana dashboard is accessible
- [ ] All 8 dashboard panels render (no "No data" errors)
- [ ] API logs show "Cost metrics initialized" on startup

## Getting Help

If you encounter issues:

1. **Check logs:**
   - API: `kubectl logs deployment/ledgerlens-api -n ledgerlens`
   - Prometheus: `kubectl logs deployment/prometheus -n monitoring`
   - Grafana: `kubectl logs deployment/grafana -n monitoring`

2. **Validate configuration:**
   - `promtool check rules monitoring/recording_rules_cost.yml`
   - `jq empty monitoring/grafana/cost_capacity_dashboard.json`

3. **Review documentation:**
   - [docs/cost_and_capacity.md](cost_and_capacity.md) — Comprehensive guide
   - [monitoring/README.md](../monitoring/README.md) — Quick reference

4. **File an issue:**
   - Include: error logs, Prometheus version, Kubernetes version
   - Redact sensitive information (cost coefficients, API keys)

## Next Steps

After successfully upgrading:

1. **Calibrate cost coefficients** with your actual cloud pricing
2. **Set up Alertmanager routing** for capacity alerts
3. **Review the dashboard weekly** to understand cost trends
4. **Tune projection window** if traffic patterns are volatile
5. **Consider additional panels** (month-over-month cost, budget tracking)

---

**Upgrade Guide Version:** 1.0  
**Last Updated:** 2026-07-18  
**Tested With:** Prometheus 2.45.0, Grafana 10.0.0, Kubernetes 1.27+
