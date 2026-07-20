# LedgerLens SLO / Error-Budget Framework

This document outlines the Service Level Objectives (SLOs), Service Level Indicators (SLIs), alerting design, and error-budget policies for LedgerLens's key user journeys.

---

## Service Level Objectives (SLOs)

LedgerLens defines four core SLOs over a rolling **30-day window**:

| User Journey | Service Level Indicator (SLI) | Target | Rationale |
|---|---|---|---|
| **Score Availability** | % of `GET /scores/{wallet}` requests returning non-5xx status codes | **99.5%** | Downstream integrators depend on real-time wallet risk scores to authorize actions on the Stellar network. |
| **Scoring Latency** | % of score evaluations completing in **< 2.0s** | **99.0%** | Keeps the interactive application responsive and prevents timeouts during inline score requests. |
| **Webhook Delivery** | % of webhook notification delivery attempts returning `result="delivered"` | **99.0%** | Ensures client backends receive immediate, reliable notification of feature-drift or risk-score spikes. |
| **Soroban Submission** | % of on-chain submissions resulting in `status="success"` (excluding dry runs) | **99.0%** | Guarantees auditability of LedgerLens classifications on the Soroban smart contract network. |

---

## Alerting Design (Multi-Window, Multi-Burn-Rate)

Instead of relying solely on single-threshold alerts (which cause alert fatigue or miss slow degradations), LedgerLens implements **multi-window, multi-burn-rate alerting** as described in the Google SRE workbook.

We monitor two classes of burn rates per SLO:

### 1. Fast-Burn Alert (Critical Page)
- **Burn Rate**: `14.4x` (will consume the entire 30-day error budget in 2 days)
- **Windows**: `1 hour` (long window) and `5 minutes` (control window)
- **Delay (`for`)**: `2 minutes`
- **Routing**: Triggers immediate paging notifications (e.g. PagerDuty, ops SMS) for developer intervention.

### 2. Slow-Burn Alert (Lower Urgency Ticket)
- **Burn Rate**: `6.0x` (will consume the entire 30-day error budget in 5 days)
- **Windows**: `6 hours` (long window) and `30 minutes` (control window)
- **Delay (`for`)**: `15 minutes`
- **Routing**: Triggers non-paging tickets (e.g. Jira ticket, Slack alert) for triage during normal business hours.

---

## Error-Budget Policy

An error budget represents the allowed unreliability of a service (e.g. `0.5%` for Score Availability). If a service exhausts its error budget (remaining budget falls below `0.0%` over the rolling 30-day window), the following policies apply:

1. **Deploy Freeze**: All non-critical feature deployments are frozen until the error budget is restored above `0.0%`.
2. **Refactoring Priority**: Engineering focus pivots entirely from feature work to performance and reliability improvements (e.g., query optimization, infrastructure scaling, RPC endpoint failover).
3. **Canary Gating**: Continuous integration pipelines block releases if the staging environment shows a degraded SLO posture.

---

## Monitoring and Operations

### Live Budget Endpoint
Operators can query `GET /admin/slo-status` (gated by `X-LedgerLens-Admin-Key`) to retrieve the current error budget consumption per SLO:

```bash
curl -H "X-LedgerLens-Admin-Key: <key>" http://localhost:8000/v1/admin/slo-status
```

### Recording Rules
Ratio metrics and budget metrics are calculated in real-time by Prometheus recording rules defined in `monitoring/recording_rules.yml`. Use Grafana dashboards linked to these rules for visual tracking of remaining budget trends.
