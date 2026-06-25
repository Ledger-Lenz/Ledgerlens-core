# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/google-github-actions/release-please-action);
merging a release PR (created by the `release-please` GitHub Action) tags the
commit, generates this file, and publishes a tagged Docker image to GHCR.

## Unreleased

### Added
- **ED25519 model signing** (`detection/model_signing.py`): `ModelSigner` class with asymmetric ED25519 sign/verify for `.joblib` model artifacts, preventing supply-chain attacks via tampered model files. Public key in `config/settings.py`; private key loaded from `MODEL_SIGNING_PRIVATE_KEY` environment variable only.
- CLI commands `generate-signing-key` and `verify-models` for keypair generation and batch signature verification.
- `docs/model_signing.md`: threat model, key management, rotation procedure, CI integration guide.
- **Uniswap V3 adapter** (`ingestion/uniswap_adapter.py`): ingests `Swap` events from Uniswap V3 pools for EVM wallets linked to Stellar accounts, extending the cross-chain detection graph. Feature-flagged via `INGEST_UNISWAP=true`.
- **Curve adapter** (`ingestion/curve_adapter.py`): ingests `TokenExchange` events from Curve pools for linked EVM wallets. Feature-flagged via `INGEST_CURVE=true`.
- **Shadow model scoring** (`detection/shadow_scoring.py`): runs a candidate model in parallel with production on every scoring request, logging score divergence to Prometheus histogram and SQLite `shadow_scores` table. `GET /admin/shadow/report` returns mean divergence, p95, and high-divergence wallets. Activated via `SHADOW_MODEL_VERSION` env var.
- **E2E test suite** (`tests/e2e/`): end-to-end tests covering ingest→score→retrieve, alert flow, and federated training rounds. Run with `make test-e2e`. Completes in < 5 minutes.

### Added
- **#147** Pedersen commitment ZK scheme (`detection/zk_commitment.py`): `PedersenParams`, `PedersenCommitment`, `ThresholdProof` dataclasses; `commit()`, `open()`, `prove_below_threshold()`, `verify_below_threshold()` functions over BN254 for privacy-preserving score attestation.
- **#147** API endpoints `POST /scores/{wallet}/commit` and `POST /scores/verify-threshold` for ZK threshold proofs.
- **#150** Full governance proposal engine (`detection/governance.py`): `GovernanceEngine` with `submit_proposal`, `cast_vote`, `tally_proposal`, `close_proposal`, `execute_proposal`, `close_expired`; `SettingsReloader` with compile-time allowlist and atomic `.env` write.
- **#150** SQLite migration 13: `governance_proposals`, `governance_votes`, `governance_committee` tables.
- **#150** Governance REST endpoints: `POST/GET /governance/proposals`, `GET /governance/proposals/{id}`, `POST /governance/proposals/{id}/vote`, `POST /governance/proposals/{id}/execute` (admin-key gated).
- **#150** `cli.py governance-close-expired` command.
- `docs/governance_protocol.md` updated to reflect full implemented lifecycle.

### Added
- Synthetic SDEX trade generator (`ingestion/synthetic_data.py`) with
  labelled wash-trading rings for local training and testing.
- Labelled training dataset builder (`detection/dataset.py`).
- SQLite-backed local `RiskScore` store (`detection/storage.py`).
- Local read-only FastAPI app (`api/main.py`) serving `/scores`, `/alerts`,
  and `/assets/risk-ranking`.
- `ledgerlens` CLI (`cli.py`): `generate-data`, `train`, `score`, `serve`.
- Retrying HTTP client for Horizon API calls (`ingestion/http_client.py`).
- Dockerfile, docker-compose, and GitHub Actions CI workflow.
- `ledgerlens --version` / `-V` flag that reports the current version from
  `pyproject.toml`.
- `release-please` GitHub Action workflow for automated semantic versioning,
  changelog generation, and Docker image publishing to GHCR.

### Fixed
- `detection/shap_explainer.py` updated for the current SHAP `TreeExplainer`
  output shape.

## 0.1.0

- Initial scaffold: Horizon ingestion, Benford's Law engine, ML feature
  engineering, ensemble model training/inference, `RiskScore` schema.
