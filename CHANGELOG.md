# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/google-github-actions/release-please-action);
merging a release PR (created by the `release-please` GitHub Action) tags the
commit, generates this file, and publishes a tagged Docker image to GHCR.

## [Unreleased]

### Added
- Multi-signature Oracle Quorum for tamper-resistant on-chain risk score publication using a 3-of-5 ED25519 threshold.
- `GET /admin/oracle/status` endpoint to monitor oracle node health and keys.
- Rust `oracle_aggregator` Soroban contract for robust on-chain threshold verification.

### Added
- **#144** `tests/test_webhook_security.py`: exhaustive webhook HMAC and security test suite — `TestHMACVerification`, `TestTimestampReplayPrevention` (freezegun), `TestSecretRotation`, `TestDeadLetterBehaviour` (exactly 8 failures, exponential backoff), `TestConcurrency`, `TestSSRFProtection`, and AST static-analysis test for `hmac.compare_digest`.
- **#144** `docs/webhook_security_model.md`: HMAC signing, replay prevention, secret rotation, dead-letter recovery, and SSRF protection documentation.
- **#147** Pedersen commitment ZK scheme (`detection/zk_commitment.py`): `PedersenParams`, `PedersenCommitment`, `ThresholdProof` dataclasses; `commit()`, `open()`, `prove_below_threshold()`, `verify_below_threshold()` functions over BN254 for privacy-preserving score attestation.
- **#147** API endpoints `POST /scores/{wallet}/commit` and `POST /scores/verify-threshold` for ZK threshold proofs.
- **#150** Full governance proposal engine (`detection/governance.py`): `GovernanceEngine` with `submit_proposal`, `cast_vote`, `tally_proposal`, `close_proposal`, `execute_proposal`, `close_expired`; `SettingsReloader` with compile-time allowlist and atomic `.env` write.
- **#150** SQLite migration 13: `governance_proposals`, `governance_votes`, `governance_committee` tables.
- **#150** Governance REST endpoints: `POST/GET /governance/proposals`, `GET /governance/proposals/{id}`, `POST /governance/proposals/{id}/vote`, `POST /governance/proposals/{id}/execute` (admin-key gated).
- **#150** `cli.py governance-close-expired` command.
- `docs/governance_protocol.md` updated to reflect full implemented lifecycle.
- **Monte Carlo bootstrap p-values for Benford chi-square** (`detection/benford_engine.py`):
  wallets with fewer than `BENFORD_BOOTSTRAP_THRESHOLD` (default 100) transactions
  in a window now use an empirical p-value derived from 10,000 multinomial samples
  drawn from the theoretical Benford distribution, eliminating false positives caused
  by asymptotic chi-square approximation failures in small-sample regimes common on
  SDEX short time windows (1h, 4h).
- `bootstrap_chi_square_pvalue` function with fully vectorised NumPy implementation
  (single `rng.multinomial` call; < 500 ms for N = 50, n = 10,000).
- `BENFORD_PROBS` numpy array constant (normalised Benford probabilities for digits 1–9).
- `BENFORD_BOOTSTRAP_THRESHOLD` and `BENFORD_BOOTSTRAP_SAMPLES` module constants,
  overridable via environment variables.
- `compute_chi_square_pvalue(counts, N) -> (p_value, method)` function that dispatches
  to bootstrap or asymptotic computation based on sample size.
- LRU cache (`maxsize=512`) on `_cached_bootstrap_pvalue` to avoid recomputing p-values
  for repeated wallet-window evaluations with the same digit counts.
- `BenfordWindowFeatures` dataclass with `chi_square_pvalue_method` field so callers and
  audit logs know whether a flagging decision used bootstrap or asymptotic estimates.
- `chi_square_pvalue` and `pvalue_method` keys added to the dict returned by
  `compute_benford_metrics` (backward-compatible; existing keys unchanged).
- `--bootstrap-threshold` and `--bootstrap-samples` CLI flags on `ledgerlens score`.
- `BENFORD_BOOTSTRAP_THRESHOLD` and `BENFORD_BOOTSTRAP_SAMPLES` documented in `.env.example`.
- `docs/benford_analysis.md` with "Small-Sample P-Value Estimation" methodology section.
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
