---
title: "Add Cross-Chain Bridge Transaction Correlation for Multi-Network Wash Detection"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary
Sophisticated wash traders bridge assets between Stellar and EVM chains to obscure the circular flow of funds. The current cross-chain detection only flags wallets with bridge events; it does not correlate the timing and amounts of bridge-in and bridge-out transactions to detect round-trip bridge patterns. Amount and timing correlation across bridge legs identifies multi-network wash cycles that single-network analysis misses.

## Objectives
- [ ] Implement `CrossChainCorrelator` in `detection/cross_chain_correlator.py`
- [ ] For each wallet, find pairs of (Stellar→EVM bridge-out, EVM→Stellar bridge-in) within a configurable time window (default 24h)
- [ ] Compute correlation score based on: amount match (within 5% after fees), timing delta, and EVM intermediate hops
- [ ] Add `cross_chain_round_trip_score` as feature #36 in the feature vector
- [ ] Write tests using synthetic bridge event sequences with known round-trip patterns

## Definition of Done
- [ ] Synthetic round-trip test wallet scores higher after feature addition
- [ ] Feature computed in < 10ms per wallet with pre-indexed bridge events
- [ ] No regression on existing 35-feature test baseline
- [ ] Feature documented in `docs/feature_reference.md`
