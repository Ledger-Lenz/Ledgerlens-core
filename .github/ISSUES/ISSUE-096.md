---
title: "Add Circuit Breaker Pattern for Horizon API and Feature Store External Calls"
labels: ["difficulty: intermediate", "area: infrastructure", "type: enhancement"]
assignees: []
---

## Summary
When the Stellar Horizon API or Redis feature store is unavailable, the LedgerLens ingestion worker retries indefinitely, exhausting connection pools and causing cascading failures. A circuit breaker that opens after N consecutive failures and half-opens after a recovery timeout prevents resource exhaustion and provides fast-fail behaviour during outages.

## Objectives
- [ ] Implement `CircuitBreaker` class in `utils/circuit_breaker.py` with `CLOSED`, `OPEN`, `HALF_OPEN` states
- [ ] Wrap Horizon HTTP client calls in `ingestion/horizon_streamer.py` with a circuit breaker (threshold: 5 failures, timeout: 60s)
- [ ] Wrap Redis feature store calls with a separate circuit breaker (threshold: 3 failures, timeout: 30s)
- [ ] Emit `circuit_open` and `circuit_closed` log events; expose state via `GET /health` response
- [ ] Use `pybreaker` library or implement from scratch

## Definition of Done
- [ ] Circuit opens after configured failure threshold and stops sending requests
- [ ] Circuit transitions to HALF_OPEN after timeout and retries one probe request
- [ ] `GET /health` shows open circuits as degraded (not failed)
- [ ] Tests simulate Horizon outage and verify circuit transitions
