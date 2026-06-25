# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/google-github-actions/release-please-action);
merging a release PR (created by the `release-please` GitHub Action) tags the
commit, generates this file, and publishes a tagged Docker image to GHCR.

## Unreleased

### Added
- **Bayesian cross-chain linking**: `CrossChainLinker.score_hypothesis()` assigns probabilistic confidence scores (0–1) to Stellar↔EVM wallet link hypotheses using timing similarity, amount matching, direction consistency, and address-pattern features. Links below confidence=0.7 are rejected.
- `WalletLinkHypothesis` dataclass and `LinkStatus` enum (`rejected`, `probable`, `confirmed`).
- SQLite `cross_chain_links` table for persisting accepted hypotheses with upsert support.
- `GET /cross-chain/links/{stellar_wallet}` endpoint returning accepted link hypotheses sorted by confidence.
- `GET /cross-chain/links/{stellar_wallet}/explain` endpoint (admin-key gated) returning evidence feature breakdown.
- `cross_chain_link_confidence` feature added to the cross-chain feature vector in `feature_engineering.py`.
- `CROSS_CHAIN_TIMING_SIGMA_SECONDS`, `CROSS_CHAIN_AMOUNT_TOLERANCE`, `CROSS_CHAIN_MIN_CONFIDENCE`, `CROSS_CHAIN_CONFIRMED_CONFIDENCE` configuration variables.
- **Backtesting framework** (`backtesting/backtest_runner.py`): loads labelled CSV datasets, runs the full feature extraction and scoring pipeline, and computes precision/recall/F1/AUC-ROC/average precision at configurable thresholds.
- `cli.py backtest run` command for running backtests from the command line.
- Synthetic labelled dataset in `data/backtest/known_cases.csv` for CI validation.

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
