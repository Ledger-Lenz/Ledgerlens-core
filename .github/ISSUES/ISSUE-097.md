---
title: "Build Alert Deduplication Engine to Suppress Repeated High-Risk Notifications"
labels: ["difficulty: intermediate", "area: detection", "type: feature"]
assignees: []
---

## Summary
When a wallet maintains a persistently high risk score, the current alerting system emits a new alert on every scoring cycle, flooding downstream consumers with duplicate notifications. A deduplication engine that tracks active alert state per wallet and only emits a new alert when the wallet transitions from non-alerting to alerting (or the score increases significantly) reduces alert fatigue.

## Objectives
- [ ] Implement `AlertDeduplicator` in `detection/alert_engine.py` tracking `(wallet, alert_active)` state in SQLite
- [ ] Emit `alert.opened` event when wallet score first exceeds threshold
- [ ] Emit `alert.escalated` event when score increases by > 10 points while alert is active
- [ ] Emit `alert.resolved` event when score drops below threshold for 3 consecutive scoring cycles
- [ ] Deduplication state survives server restarts (persisted in SQLite)

## Definition of Done
- [ ] Wallet with stable high score generates exactly 1 `alert.opened` event, not one per cycle
- [ ] Score increase triggers `alert.escalated` event
- [ ] Resolution requires 3 consecutive below-threshold cycles (hysteresis)
- [ ] Tests cover all three state transitions
