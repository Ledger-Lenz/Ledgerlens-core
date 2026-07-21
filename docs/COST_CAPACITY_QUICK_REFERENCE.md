# Cost & Capacity Quick Reference

A one-page cheat sheet for LedgerLens cost visibility and capacity planning.

## Configuration

```bash
# .env / Helm values
COST_PER_VCPU_HOUR_USD=0.0416          # $/vCPU-hour
COST_PER_GB_MEMORY_HOUR_USD=0.0056     # $/GB-hour
COST_PER_GB_STORAGE_MONTH_USD=0.10     # $/GB-month
CAPACITY_PROJECTION_WINDOW_DAYS=7      # Lookback for projection
CAPACITY_PROJECTION_LEAD_TIME_DAYS=14  # Alert threshold
```

## Key Metrics

| Metric | Description | Unit |
|--------|-------------|------|
| `ledgerlens:pod_cost_per_hour:usd` | Per-pod cost (CPU + memory) | USD/hr |
| `ledgerlens:namespace_cost_per_hour:usd` | Total namespace cost | USD/hr |
| `ledgerlens:cost_per_wallet_scored:usd` | Unit cost per wallet | USD |
| `ledgerlens:replica_count_projected_days_to_max` | Days until maxReplicas | days |
| `ledgerlens:pvc_projected_days_to_full` | Days until PVC full | days |
| `ledgerlens:wallets_scored_per_hour` | Current throughput | wallets/hr |

## Dashboard Panels

1. **Namespace Cost Per Hour** — Total cost trend
2. **Cost Per Wallet Scored** — Unit economics
3. **Cost Per Pod** — Per-pod breakdown
4. **Days Until Max Replicas** — Capacity gauge (⚠️ < 14d)
5. **Days Until PVC Full** — Storage capacity gauge (⚠️ < 14d)
6. **Wallet Scoring Throughput** — Current vs projected
7. **API Replica Count** — Autoscaling status
8. **PVC Usage %** — Storage utilization

## Alert: CapacityLimitApproaching

**Fires when:** Days-to-limit < 14 (replica count OR PVC usage)  
**For:** 1 hour

**Runbook:**
1. Open dashboard → check "Days Until Max Replicas" / "Days Until PVC Full"
2. Review "Wallet Scoring Throughput" — expected or anomalous?
3. **If expected:** Scale up
   ```bash
   # Raise maxReplicas
   helm upgrade ledgerlens --set autoscaling.maxReplicas=20
   
   # Or expand PVC (if storage class supports it)
   kubectl edit pvc ledgerlens -n ledgerlens
   # Update spec.resources.requests.storage: 20Gi
   ```
4. **If unexpected:** Investigate ingestion volume, check for duplicate trades

## Quick Checks

```bash
# Verify cost metrics are exposed
curl http://localhost:8000/metrics | grep ledgerlens_cost_per

# Check recording rules are loaded
curl -s http://localhost:9090/api/v1/rules | \
  jq '.data.groups[] | select(.name == "ledgerlens_cost")'

# Query current namespace cost
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:namespace_cost_per_hour:usd' | \
  jq -r '.data.result[0].value[1]'

# Check days until maxReplicas
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=ledgerlens:replica_count_projected_days_to_max' | \
  jq -r '.data.result[0].value[1]'
```

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Cost metrics are zero | Cost gauges not initialized | Check API logs for "Cost metrics initialized"; restart API |
| Projection is NaN | < 7 days of data | Wait for data to accumulate or reduce projection window |
| Projection is +Inf | No growth | Expected for stable workloads |
| Alert not firing | < 1 hour sustained | Wait; alert has 1h `for` duration |
| Dashboard shows "No data" | Wrong namespace label | Edit dashboard, change `namespace="ledgerlens"` to match yours |

## File Locations

- **Recording rules:** `monitoring/recording_rules_cost.yml`
- **Alerts:** `monitoring/alerts.yml`
- **Dashboard:** `monitoring/grafana/cost_capacity_dashboard.json`
- **Cost exporter:** `config/cost_exporter.py`
- **Helm config:** `helm/ledgerlens/templates/cost-config.yaml`

## Prometheus Rule Validation

```bash
# Validate recording rules
promtool check rules monitoring/recording_rules_cost.yml

# Validate alerts
promtool check rules monitoring/alerts.yml
```

## Grafana Dashboard Import

```bash
# Manual: Grafana UI → Dashboards → Import → Upload JSON

# Provisioned:
sudo cp monitoring/grafana/cost_capacity_dashboard.json \
  /var/lib/grafana/dashboards/ledgerlens/
sudo systemctl restart grafana-server
```

## Capacity Thresholds

| Days to Limit | Status | Action |
|---------------|--------|--------|
| < 7 | 🔴 Critical | Scale now |
| 7-14 | 🟠 Warning | Plan expansion |
| 14-30 | 🟡 Monitor | Review next week |
| > 30 | 🟢 Healthy | No action |

## Cost Estimation Formulas

```
# Daily cost
Daily = namespace_cost_per_hour × 24

# Monthly cost
Monthly = namespace_cost_per_hour × 730

# Cost per 1K wallets scored
Per1K = cost_per_wallet_scored × 1000

# ROI (if charging per score)
ROI = (revenue_per_wallet - cost_per_wallet_scored) / cost_per_wallet_scored × 100%
```

## Security Notes

- ⚠️ Do NOT commit actual negotiated cloud pricing to public repos
- 🔒 Enable Grafana auth (disable anonymous access)
- 🛡️ Protect `/metrics` with `LEDGERLENS_ADMIN_API_KEY`

## Documentation Links

- **Full guide:** [docs/cost_and_capacity.md](cost_and_capacity.md)
- **Upgrade guide:** [docs/UPGRADE_COST_CAPACITY.md](UPGRADE_COST_CAPACITY.md)
- **Monitoring README:** [monitoring/README.md](../monitoring/README.md)
- **Observability:** [docs/observability.md](observability.md)

---

**Quick Reference Version:** 1.0  
**Last Updated:** 2026-07-18
