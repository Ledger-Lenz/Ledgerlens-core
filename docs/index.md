# LedgerLens Core

LedgerLens is a Benford's Law + ensemble ML wash-trading detection engine for the Stellar DEX.

## What it does

LedgerLens ingests real-time trade data from the Stellar Horizon API and scores wallet/asset-pair
combinations for wash-trading risk using a multi-layer detection pipeline:

- **Benford's Law analysis** — statistical digit-distribution tests on trade amounts
- **Ensemble ML** — Random Forest, XGBoost, and LightGBM ensemble scoring
- **Graph analysis** — wash-trading ring detection via network graph algorithms
- **Cross-chain detection** — EVM bridge transfer correlation
- **Temporal modeling** — LSTM-based temporal risk adjustment
- **Conformal prediction** — calibrated uncertainty bands on scores

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Generate synthetic training data and train models
make generate-data
make train

# Start the local API
make serve
```

The API is then available at `http://localhost:8000`. See the [REST API reference](api_reference.md)
for all available endpoints.

## Architecture

```
Horizon SSE Stream → Ingestion → Feature Store (Redis + SQLite)
                                        ↓
                              Detection Engine (Benford + ML + Graph)
                                        ↓
                              Risk Score (0–100) → API + Webhooks
                                        ↓
                              On-chain submission (Soroban)
```

## Documentation Sections

| Section | Description |
|---------|-------------|
| [Feature Store](feature_store.md) | Redis hot layer + SQLite cold layer incremental feature computation |
| [Cross-Chain Detection](cross_chain_detection.md) | EVM bridge transfer correlation and wallet linking |
| [Federated Learning](federated_learning.md) | Privacy-preserving distributed model training |
| [Adversarial Robustness](adversarial_robustness.md) | Red team, evasion detection, model hardening |
| [Uncertainty Quantification](uncertainty_quantification.md) | Conformal prediction intervals |
| [Governance Protocol](governance_protocol.md) | Dispute resolution and committee voting |
| [Oracle Network](oracle_network.md) | Decentralised oracle node architecture |

## License

[Apache 2.0](https://github.com/Ledger-Lenz/Ledgerlens-core/blob/main/LICENSE)
