---
title: "Build Realistic Trade Sequence Factory for Deterministic Test Data Generation"
labels: ["difficulty: intermediate", "area: testing", "type: feature"]
assignees: []
---

## Summary
Tests across the codebase construct `Trade` objects ad hoc with hard-coded amounts and timestamps, making tests brittle and hard to read. A `TradeFactory` class that generates realistic trade sequences — with configurable wash patterns, asset pairs, timing distributions, and amounts — provides a single source of truth for test data and enables scenario-driven testing.

## Objectives
- [ ] Implement `tests/factories.py` with `TradeFactory` supporting: `wash_ring(n_accounts, n_rounds)`, `legitimate_market_maker(n_trades)`, `spoofing_attack(n_layers)`, `random_noise(n_trades, seed=42)`
- [ ] All factory methods return `list[Trade]` with realistic paging_tokens and ledger sequences
- [ ] Factory outputs are deterministic given the same seed
- [ ] Migrate at least 20 existing tests to use `TradeFactory` instead of inline dicts
- [ ] Document factory in `docs/testing_guide.md`

## Definition of Done
- [ ] All four factory scenarios implemented with configurable parameters
- [ ] Factory outputs verified against real Horizon API responses for schema compatibility
- [ ] 20+ tests migrated; no test regressions
- [ ] `TradeFactory.wash_ring(5, 10)` produces a detectable ring when scored
