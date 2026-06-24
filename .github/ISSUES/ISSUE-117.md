---
title: "Build Chaos Engineering Test Suite for Resilience Validation Under Component Failure"
labels: ["difficulty: advanced", "area: testing", "type: feature"]
assignees: []
---

## Summary
LedgerLens has no automated resilience testing. Failures in the Horizon API, Redis, or SQLite are handled with retry logic and circuit breakers, but these mechanisms have never been validated under realistic failure conditions. A chaos engineering test suite using `toxiproxy` or `chaos-monkey`-style fault injection verifies that the system degrades gracefully rather than cascading.

## Objectives
- [ ] Implement `tests/chaos/` with Toxiproxy-controlled network fault injection
- [ ] Chaos scenario: Horizon API latency spike (500ms added) → scoring latency increases but does not exceed 2s p99
- [ ] Chaos scenario: Redis connection refused → feature store falls back to cold tier without error
- [ ] Chaos scenario: SQLite WAL locked → API returns 503 with `Retry-After`, not an unhandled 500
- [ ] Chaos scenario: partial network partition → circuit breaker opens within 5 failures

## Definition of Done
- [ ] All four chaos scenarios pass (system degrades gracefully in each case)
- [ ] Chaos tests run as optional CI job (`make test-chaos`) using Docker Compose + Toxiproxy
- [ ] Each scenario includes a recovery phase: fault removed → system returns to healthy within 60s
