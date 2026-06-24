---
title: "Build Solana SPL Token Trade Ingestion Adapter"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
LedgerLens currently ingests Stellar SDEX and EVM bridge events but has no visibility into Solana-side trading activity for wallets that operate cross-chain via Wormhole. A Solana SPL token trade ingestion adapter using the Solana RPC `getSignaturesForAddress` API adds cross-chain coverage for Stellar↔Solana bridge users.

## Objectives
- [ ] Implement `SolanaAdapter` in `ingestion/solana_adapter.py` using `solana-py` library
- [ ] Ingest SPL token swap events from Serum/OpenBook DEX for linked Solana addresses
- [ ] Map Solana trades to the canonical `Trade` dataclass (with `source="solana"`)
- [ ] Cross-reference via Wormhole bridge event VAAs to link Stellar↔Solana wallet pairs
- [ ] Configurable via `SOLANA_RPC_URL` env var; support both mainnet-beta and devnet

## Definition of Done
- [ ] Adapter ingests SPL swap events for a test Solana address
- [ ] Solana trades appear in the feature store alongside Stellar trades
- [ ] Wormhole VAA parsing links Stellar G... address to Solana pubkey
- [ ] Tests use VCR cassettes to mock Solana RPC responses
