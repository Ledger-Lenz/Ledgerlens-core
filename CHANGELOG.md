# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

## Unreleased

### Added
- **ED25519 model signing**: `ModelSigner` class in `detection/model_signing.py`
  signs model artifacts at training time and verifies signatures at inference
  load time. Replaces HMAC-SHA256 with asymmetric ED25519 keys for stronger
  supply-chain protection.
- CLI commands `generate-signing-key` and `verify-models` for key management
  and CI integration.
- `MODEL_SIGNING_PUBLIC_KEY` setting in `config/settings.py`; private key
  loaded from `MODEL_SIGNING_PRIVATE_KEY` environment variable only.
- Documentation: `docs/model_signing.md` covering threat model, key management,
  rotation procedure, and CI integration.
- **Uniswap V3 adapter** (`ingestion/uniswap_adapter.py`): Ingests Swap events
  from Uniswap V3 pools, filtered to wallets linked to Stellar via bridge graph.
- **Curve adapter** (`ingestion/curve_adapter.py`): Ingests TokenExchange events
  from Curve StableSwap pools for cross-chain wash-cycle detection.
- Feature flags `INGEST_UNISWAP` and `INGEST_CURVE` for opt-in DEX ingestion.
- Synthetic SDEX trade generator (`ingestion/synthetic_data.py`) with
  labelled wash-trading rings for local training and testing.
- Labelled training dataset builder (`detection/dataset.py`).
- SQLite-backed local `RiskScore` store (`detection/storage.py`).
- Local read-only FastAPI app (`api/main.py`) serving `/scores`, `/alerts`,
  and `/assets/risk-ranking`.
- `ledgerlens` CLI (`cli.py`): `generate-data`, `train`, `score`, `serve`.
- Retrying HTTP client for Horizon API calls (`ingestion/http_client.py`).
- Dockerfile, docker-compose, and GitHub Actions CI workflow.

### Fixed
- `detection/shap_explainer.py` updated for the current SHAP `TreeExplainer`
  output shape.

## 0.1.0

- Initial scaffold: Horizon ingestion, Benford's Law engine, ML feature
  engineering, ensemble model training/inference, `RiskScore` schema.
