---
title: "Add Uniswap V3 and Curve Finance Pool Event Ingestion"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
EVM-side wash traders often cycle funds through Uniswap V3 or Curve pools between Allbridge bridge hops to break amount correlation. Ingesting Uniswap V3 `Swap` events and Curve `TokenExchange` events for EVM addresses linked to Stellar wallets extends the cross-chain detection graph and enables detection of DEX-interleaved wash cycles.

## Objectives
- [ ] Implement `UniswapV3Adapter` in `ingestion/uniswap_adapter.py` consuming `Swap(address,address,int256,int256,uint160,uint128,int24)` events
- [ ] Implement `CurveAdapter` in `ingestion/curve_adapter.py` consuming `TokenExchange` events for major pools
- [ ] Filter events to only wallets linked to Stellar accounts via the bridge event graph
- [ ] Map EVM swap events to canonical `Trade` dataclass with `source="uniswap_v3"` or `"curve"`
- [ ] Add `INGEST_UNISWAP=true` and `INGEST_CURVE=true` feature flags

## Definition of Done
- [ ] Uniswap V3 swaps appear in feature store for test EVM address
- [ ] Curve exchanges appear in feature store
- [ ] Feature flags disable ingestion cleanly without error
- [ ] Tests use VCR cassettes for deterministic RPC replay
