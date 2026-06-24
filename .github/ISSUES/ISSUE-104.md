---
title: "Implement Base and Arbitrum L2 EVM Trade Ingestion Adapters"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
The EVM bridge ingestion currently covers Ethereum mainnet and Polygon. As Allbridge expands to Base and Arbitrum, wash traders are beginning to exploit these lower-fee L2 chains to reduce the cost of round-trip laundering. Adding Base and Arbitrum adapters to `ingestion/evm_loader.py` extends cross-chain detection coverage to these networks.

## Objectives
- [ ] Extend `ingestion/evm_loader.py` to support `network: str` parameter (`ethereum`, `polygon`, `base`, `arbitrum`)
- [ ] Add `BASE_RPC_URL` and `ARBITRUM_RPC_URL` configuration with fallback to public endpoints
- [ ] Deploy Allbridge contract ABI decoder for Base and Arbitrum event formats (may differ from mainnet)
- [ ] Implement per-network rate limiting (Base: 10 req/s, Arbitrum: 10 req/s)
- [ ] Test against Base Goerli and Arbitrum Goerli testnet endpoints

## Definition of Done
- [ ] Bridge events ingested from Base and Arbitrum mainnet in integration test
- [ ] Per-network circuit breakers: Base outage does not affect Arbitrum ingestion
- [ ] ABI mismatch between networks handled gracefully (log warning, skip event, do not crash)
