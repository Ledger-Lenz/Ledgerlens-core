# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases were previously automated via [release-please](https://github.com/google-github-actions/release-please-action);
the workflow has since been removed, so version bumps and this file are
currently maintained by hand until an automated release pipeline is
reinstated (tracked in [ROADMAP.md](ROADMAP.md)).

## [Unreleased]

### Fixed
- **Krum/Multi-Krum wired into production federated aggregation** (`detection/federated/server.py`,
  new `_select_krum_survivors`): `FederatedAggregationServer._aggregate_locked()` previously ran plain
  weighted FedAvg plus a historical-cosine heuristic that compares only against a rolling baseline and
  skips entirely on round 1 — `KrumStrategy` (`detection/federated/krum.py`) was defined and tested but
  never invoked from the live aggregation path. Krum/Multi-Krum peer-distance selection is now default-on
  (`settings.federated_use_krum`) and supplements the cosine heuristic, closing the round-1 / "boiling
  frog" gap. `f`/`m` are derived each round from the live valid-update count, not a static config value,
  with a documented, logged fallback to plain FedAvg when a round has too few participants for any
  tolerance. Also fixes `KrumStrategy`'s default constructor, which previously raised `ValueError` on
  `KrumStrategy()` with no arguments (`min_clients=3` gave `f=1`, and `2(1)+2=4 < 3` is false); default
  `min_clients` corrected to 5, the smallest value for which the default `f = floor(min_clients / 3)`
  derivation is self-consistent. See `docs/byzantine_resilience.md` for the full ordering rationale
  against DP noise and gradient clipping.
- **Distributed per-API-key rate limiting** (`detection/rate_limiter.py`, new): replaces
  three independent, non-communicating in-process sliding-window dicts
  (`api/gateway.py`, `detection/api_key_store.py`, each replicated per REST pod and
  never shared with the separate gRPC process) with a single Redis-backed sliding-window
  counter shared by every enforcement path (`api/gateway.py`, `api/api_key_router.py`,
  `api/grpc_scoring_service.py`). Fixes the ~2x same-process REST/gRPC budget bypass and
  the `configured_limit x N_replicas` bypass under this project's documented 2–10
  replica Helm topology. Falls back to the old per-process behavior (logged + metered)
  if Redis is unreachable. See `docs/waf_and_rate_limiting.md`.
- `config/settings.py`: `soroban_submission_lease_enabled: bool = true` was invalid
  Python (`NameError` at import time), breaking every import of `config.settings` and
  transitively the entire API and test suite. Fixed to `= True`.
- `detection/feature_engineering.py`: a name collision between a new Numba-JIT
  burst-overlap helper and the pre-existing public `cross_pair_features()` had corrupted
  the latter's `def` line into `return results -> dict:` (`SyntaxError`), silently merging
  its body with the JIT helper's. The JIT helper is renamed to
  `_cross_pair_burst_overlap_by_pair`; `cross_pair_features(account, trades_by_pair,
  correlated_pairs, cross_pair_wallets)` is restored to its documented signature.
- `detection/counterfactual_constraints.py` / `detection/counterfactual_translator.py`:
  added the three heterogeneous-GNN feature names
  (`gnn_asset_mediated_ring_score`, `gnn_order_cancel_coordination_score`,
  `gnn_funding_proximity_score`) that were missing from both modules' completeness
  manifests, which raised `RuntimeError` at import time.
- `detection/soroban_lease.py`: the `kubernetes` client import was unconditional at
  module scope despite `kubernetes` never being an installed dependency; made lazy
  (imported only when `SOROBAN_SUBMISSION_LEASE_ENABLED=true`) and added `kubernetes` to
  `requirements.txt`, matching this codebase's existing lazy-import convention for
  optional heavy dependencies.
- `api/main.py`: updated `GraphQLRouter(schema, graphiql=False)` to the current
  `strawberry-graphql` API (`graphql_ide=None`); pinned `strawberry-graphql` and `redis`
  (used by `detection/rate_limiter.py` and already assumed-but-never-declared by
  `detection/feature_store.py`) in `requirements.txt`.
- `generated/scoring_pb2.py` / `scoring_pb2_grpc.py`: regenerated against the
  `protobuf` version now pinned in `requirements.txt` — the checked-in gencode required
  a newer protobuf runtime than the project's other dependencies (`grpcio-tools`,
  `databricks-sdk`/mlflow) support, so any import of the gRPC scoring service raised
  `VersionError`.

  These six fixes were prerequisites, not scope creep: `api/main.py` (and therefore
  `tests/test_waf_middleware.py`, `tests/test_api_gateway.py`, and the gRPC test suite)
  could not be imported at all before them.

### Removed
- `api/adaptive_rate_limiter.py`: unreachable from any real request (its only caller,
  `api/auth.py`'s `require_api_key_scope`, was itself dead code never imported by any
  router) and independently broken (referenced three undefined functions). Rewiring it
  would have required a second, parallel distributed abuse-signal counter; its purpose is
  largely subsumed by the primary limiter now correctly enforcing configured limits
  across every replica and protocol. See `docs/waf_and_rate_limiting.md`.

### Added
- **Feature Store cold-tier archival to Parquet** (`detection/feature_store.py`):
  `FeatureStoreArchiver.archive_old_features(cutoff_days=30)` moves rows older than
  the cutoff from `feature_distribution_snapshots` (SQLite) to date-partitioned Parquet
  files under `FEATURE_ARCHIVE_DIR`, eliminating the previous hard cap of 500 000 rows
  while preserving full history for 60–90 day drift analysis.
- `ParquetFeatureColdTier` class: reads archived Parquet data with PyArrow filter pushdown.
- `DualTierFeatureStore` class: unified `query()` interface over both SQLite hot tier and
  Parquet cold tier; deduplicates by `(wallet, feature_name, recorded_at)` and logs a
  WARNING when duplicates are detected (indicates a previously failed archive run).
- `FeatureStore.query()` method: filter-capable read from `feature_distribution_snapshots`.
- `load_production_features(store, since_days)` in `detection/drift_monitor.py`: replaces
  direct SQLite reads so drift-analysis callers receive data from both storage tiers
  transparently.
- `cli.py archive-features` command: manually trigger cold-tier archival.
- Archival integrated into `cli.py retrain-check`: runs at the start of each check.
- `GET /admin/feature-store/stats` endpoint: returns hot-tier row count, cold-tier row
  count, oldest record timestamps, and archive directory size in MB.
- `FEATURE_ARCHIVE_DIR` and `FEATURE_ARCHIVE_CUTOFF_DAYS` configuration variables
  documented in `.env.example`.
- `docs/feature_store_archival.md`: tiered storage architecture, Parquet partition layout,
  archival schedule, and recovery procedure for failed archives.

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
- **Adversarial trade data generators** (`ingestion/adversarial_data.py`): four specialist wash-trade generators that simulate sophisticated evasion strategies — `BenfordCamouflageGenerator` (Benford-conforming amounts via leading-digit sampling), `TimingJitterGenerator` (Poisson-process inter-arrival times), `GraphFragmentationGenerator` (isolated 3-node SCCs with GFRAG-prefixed synthetic wallets), `CrossPairRotationGenerator` (volume rotation across XLM/USDC, XLM/yXLM, USDC/yUSDC, XLM/AQUA, USDC/AQUA).
- `AdversarialDataset` class in `ingestion/adversarial_data.py`: combines any evasion generator with normal background trades and runs the full feature pipeline to produce a labelled `FEATURE_NAMES + label` DataFrame for recall evaluation.
- `BENFORD_PROBS` and `ASSET_PAIRS` constants; `_resolve_pair()` helper for multi-asset-pair trade construction.
- `cli.py generate-adversarial` command: writes adversarial feature CSVs to disk with `--label-wash/--label-clean` safety flag; supports all four evasion strategies.
- `tests/test_adversarial_detection.py`: 16 tests covering Benford conformity (chi-square p > 0.05), timing jitter distribution (CoV ≈ 1.0, mean within 20 %), graph fragmentation SCC size (≤ 3 nodes), cross-pair coverage (all 5 pairs present), positive-amount guards, feature completeness assertions, and parameterised recall tests asserting ≥ 60/65/55/60 % recall on each evasion strategy.
- `docs/adversarial_testing.md`: strategy descriptions, recall threshold table, nightly CI integration guide, CLI usage examples, adversarial retraining instructions, and how to add new evasion strategies.
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
