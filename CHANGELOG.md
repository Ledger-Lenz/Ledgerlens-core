# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/google-github-actions/release-please-action);
merging a release PR (created by the `release-please` GitHub Action) tags the
commit, generates this file, and publishes a tagged Docker image to GHCR.

## [Unreleased]

### Added
- **Iterative Tarjan SCC ring detector** (`detection/graph_engine.py`): `IterativeTarjanSCC` replaces the implicit recursive Tarjan inside `networkx.strongly_connected_components` with an explicit work-stack, eliminating Python's `RecursionError` for graphs with more than ~1 000 nodes in a single SCC.
- `NodeIndex` class: O(1) bijective `str↔int` mapping for Stellar account identifiers, used by `IterativeTarjanSCC` and `SparseTradeGraph`.
- `SparseTradeGraph` class: `scipy.sparse.csr_matrix`-backed adjacency for graphs with `n_nodes >= GRAPH_MMAP_THRESHOLD` (default 50 000). `build_from_trades(trades)` constructs the CSR matrix from a list of `Trade` records; `to_adjacency_dict()` converts it back to an adjacency dict for Tarjan traversal.
- `TradeGraph` class: public incremental API (`add_trade`, `find_wash_rings`, `get_ring_members`) that selects CSR or dict adjacency automatically based on node count. Produces identical ring output to the existing module-level `find_wash_rings` function.
- `GraphTooLargeError`: raised by `TradeGraph.add_trade` and `SparseTradeGraph.build_from_trades` when the node count exceeds `MAX_GRAPH_NODES` (default 1 000 000) to prevent runaway memory allocation.
- `GRAPH_MMAP_THRESHOLD` and `MAX_GRAPH_NODES` configuration variables (overridable via environment variables; documented in `.env.example`).
- `docs/performance.md`: profiling results table for 10 K / 100 K node graphs. Measured result: **100 K nodes + 500 K edges in ~27 s, 62 MB peak RAM** on a single CPU core (target: < 30 s, < 500 MB).
- `tests/test_iterative_tarjan.py`: 27 new tests covering SCC correctness, recursion-limit elimination (2 000-node chain), self-loop safety, disconnected graphs, `NodeIndex` bijection, `SparseTradeGraph.to_adjacency_dict`, `GraphTooLargeError`, `TradeGraph` public API, output equivalence with the module-level function, and a `@pytest.mark.slow` performance test.
- Fixed pre-existing `PydanticUserError` in `config/settings.py` (`valid_sar_min_score`, `valid_export_rate_limit` validators referenced fields not present in the model; added `check_fields=False`).
- `slow` pytest mark registered in `pyproject.toml` for the 100 K-node performance test.
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
