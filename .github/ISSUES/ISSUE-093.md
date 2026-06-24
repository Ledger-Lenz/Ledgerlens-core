---
title: "Add Property-Based Testing with Hypothesis for Feature Engineering Functions"
labels: ["difficulty: intermediate", "area: testing", "type: enhancement"]
assignees: []
---

## Summary
Feature engineering functions in `detection/feature_extractor.py` operate on arbitrary trade amounts and timestamps. Unit tests only verify specific hand-crafted examples. Property-based testing with Hypothesis generates thousands of random inputs, discovering edge cases (e.g., zero-amount trades, future timestamps, single-trade wallets) that would otherwise escape notice.

## Objectives
- [ ] Add `hypothesis` to dev dependencies
- [ ] Write property tests for: `extract_benford_features()` (result always in [0,1] range), `compute_ring_score()` (monotone in ring size), `normalise_features()` (output always in [0,1])
- [ ] Write property test: for any non-empty trade batch, scoring pipeline returns a score in [0,100]
- [ ] Fix any bugs discovered by Hypothesis (document in commit message)
- [ ] Property tests run as part of `make test`

## Definition of Done
- [ ] ≥ 5 property-based test functions across the feature engineering module
- [ ] All property tests pass with `--hypothesis-seed 0` for reproducibility in CI
- [ ] At least one bug or edge case discovered and fixed during implementation
