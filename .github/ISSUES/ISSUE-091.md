---
title: "Build Scoring Pipeline Performance Benchmark with Profiling and Baseline Tracking"
labels: ["difficulty: intermediate", "area: performance", "type: feature"]
assignees: []
---

## Summary
There is no systematic performance benchmark for the LedgerLens scoring pipeline, making it impossible to detect performance regressions between releases. A benchmark suite measuring p50/p95/p99 latency for single-wallet scoring, batch scoring, and feature extraction — with results committed to `benchmarks/baseline.json` — enables regression detection in CI.

## Objectives
- [ ] Create `benchmarks/` directory with `benchmark_scoring.py` using `pytest-benchmark`
- [ ] Benchmark: single-wallet score from pre-computed features (target p99 < 50ms)
- [ ] Benchmark: feature extraction from raw trade batch of 1000 trades (target p99 < 200ms)
- [ ] Benchmark: batch scoring of 100 wallets (target: linear scaling from single-wallet baseline)
- [ ] CI job: run benchmarks and compare to `benchmarks/baseline.json`; fail if p99 regresses > 20%
- [ ] `make benchmark` updates the baseline file

## Definition of Done
- [ ] Benchmarks run reproducibly in CI (pinned random seeds, fixed trade counts)
- [ ] Baseline committed for all three scenarios
- [ ] CI benchmark job catches an artificially introduced 2× regression
- [ ] Results include hardware fingerprint (CPU model, RAM) for cross-machine comparability
