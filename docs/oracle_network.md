# LedgerLens Decentralized Oracle Network (DON)

## Overview
Multi-validator consensus mechanism for risk score publication to Soroban.

## Architecture
- **Oracle Aggregation Contract**: Soroban smart contract that handles score aggregation
- **Validator Nodes**: Independent Python nodes that compute and submit scores
- **Consensus**: Median aggregation with configurable quorum (default 3 validators)

## Validator Registration
Validators must register their Stellar public key and stake minimum XLM bond.

## Configuration
```ini
[oracle]
mode = "don"  # or "standalone" for single-publisher mode
min_validators = 3
slash_threshold = 20  # points
round_deadline_ledgers = 100
```

## References
- [Chainlink DON Architecture](https://docs.chain.link/architecture-overview/architecture-decision-log/ocr-high-level-architecture)
- [Soroban Contract Development](https://developers.stellar.org/learn/smart-contracts)
