# Wash-trading detection dashboard runbook

Dashboard: **LedgerLens Wash-Trading Detection** (uid `ledgerlens-wash-trading`),
source `monitoring/grafana/wash_trading_detection_dashboard.json`. It loads
automatically through the provider in `monitoring/grafana/provisioning/`.

Every panel shares one time axis and a shared crosshair (`graphTooltip: 1`).
Hovering over a spike in one panel marks the same instant in all the others.
Use the **Asset pair** variable to focus the detection panels on specific pairs.

## Panels

| Row | Panel | Source metric(s) | Healthy looks like |
|---|---|---|---|
| Detection signals | Benford anomaly rate | `ledgerlens_benford_flags_total` / `ledgerlens_wallets_scored_total` | Stable per-pair baseline |
| Detection signals | Drift monitor status | `ledgerlens_drift_detected_total` | No bars |
| Detection signals | Alert volume | `ledgerlens_wallets_scored_total{result="above_threshold"}`, `ledgerlens_webhook_deliveries_total` | Tracks the Benford rate; no `dead_lettered` webhooks |
| On-chain publication | Publication latency | `ledgerlens_soroban_submission_latency_seconds` | p95 steady |
| On-chain publication | Publication backlog | `ledgerlens_chain_submission_backlog`, `ledgerlens_soroban_submissions_total` | Backlog drains to 0 |
| Event bus dead letters | Dead-lettered events / Oldest dead letter age | `ledgerlens_event_bus_dead_letter_*` | 0 |

## Annotations

- **Model promotions** (green): `POST /admin/models/{version}/promote`
  (`ledgerlens_model_lifecycle_events_total{action="promote"}`).
- **Model rollbacks** (red): `detection.model_registry.rollback_model`
  (`ledgerlens_model_lifecycle_events_total{action="rollback"}`).

Annotations resolve to about 1 minute. Toggle them from the dashboard's
annotation controls.

## Triage guide

| Pattern | Likely cause | Action |
|---|---|---|
| Benford rate and alert volume jump together **right after a promotion annotation** | New model or threshold regression | Compare with the previous version. Roll back per `docs/runbooks/rollback.md`. A red annotation confirms the rollback. |
| Benford rate jumps on **all pairs at once**, with no annotation, and drift bars appear | Upstream data shift (Horizon changes, ingestion gap) | Check ingestion health and `docs/drift_monitor.md`. Do not escalate as wash trading yet. |
| Benford rate jumps on **one or a few pairs** with no drift | Genuine suspicious activity | Hand off to analysts via case management (`docs/case_management.md`). |
| Alert volume up, publication backlog growing, latency p95 rising | Soroban RPC degradation. Scores are computed but not published. | Check the `SorobanCircuitBreakerOpen` alert and RPC health. The backlog drains automatically once the RPC recovers. |
| Dead-lettered events > 0 | Kafka/NATS outage | Fix the broker, then run `ledgerlens event-bus-replay` (see `docs/event_bus.md`). |
| Alert volume drops to 0 while scoring continues | Threshold raised or model regression | Check `GET /admin/config` for `risk_score_threshold` and the latest promotion annotation. |
