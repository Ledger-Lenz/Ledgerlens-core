---
title: "Implement Alert Suppression Rules for Whitelisted Accounts and Known AMM Bots"
labels: ["difficulty: intermediate", "area: detection", "type: feature"]
assignees: []
---

## Summary
Known-legitimate high-volume accounts — DEX arbitrage bots, AMM liquidity managers, Stellar anchor wallets — score high on wash-trading metrics due to their legitimate repetitive trading patterns. An alert suppression rule engine that allows operators to whitelist specific wallets or account patterns prevents false alerts on known-good actors.

## Objectives
- [ ] Implement `SuppressionsStore` in `detection/suppressions.py` backed by SQLite `alert_suppressions` table
- [ ] `POST /admin/suppressions` to add a suppression rule: `{wallet, reason, expires_at}`
- [ ] `GET /admin/suppressions` to list active rules; `DELETE /admin/suppressions/{id}` to remove
- [ ] Apply suppressions in `AlertDeduplicator` before emitting any alert event
- [ ] Log suppression application with wallet, rule ID, and reason
- [ ] Suppression rules expire automatically at `expires_at` (UTC)

## Definition of Done
- [ ] Suppressed wallet generates no alert events regardless of score
- [ ] Expiry enforced: suppression inactive after `expires_at`
- [ ] Audit log records every suppression application
- [ ] Tests cover: suppressed wallet, expired suppression (re-alerts), suppression deletion
