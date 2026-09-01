# Changelog

All notable changes to `ledgerlens-fl-client` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No changes pending release yet._

## [0.1.0] — 2026-06-26

Initial release of the standalone federated learning client library for
exchange-side participation in the LedgerLens federated learning network
(implements [Issue #139](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/139)).

### Added

- `FLClient` — high-level client class that orchestrates a full federated
  learning round: register → fetch global model → train local ensemble →
  compute soft labels on public dataset → clip & inject DP noise → sign &
  submit update → apply distillation from updated global model.
- `DataAdapter` — abstract base class (`trade_batches()`) for exchange
  partners to supply their private trade data without exposing raw records.
- `CSVDirectoryAdapter` — convenience `DataAdapter` implementation that
  reads all `*.csv` files from a directory, sorted by filename.
- `RoundResult` dataclass — captures `round_id`, `accepted`, `reason`,
  `local_auc`, `n_samples`, `n_valid_pending`, and `quorum` for each round.
- `ClientStatus` dataclass — snapshot of operator ID, rounds completed,
  model availability, and base64-encoded DER public key.
- Differential privacy support: configurable `(ε, δ)` Gaussian mechanism
  via `dp_epsilon` / `dp_delta`; RDP path via `noise_multiplier > 0`.
- Gradient clipping: L2-norm clip via `gradient_clip_threshold` before
  noise injection.
- Ed25519 authentication: per-round payload signing with an auto-generated
  keypair; public key registered with the aggregation server on first round.
- RF/XGBoost/LightGBM ensemble with configurable per-model weights
  (`ensemble_weight_rf`, `ensemble_weight_xgb`, `ensemble_weight_lgbm`).
- Warm-starting: XGBoost and LightGBM boosters are carried forward between
  rounds to avoid cold-start retraining from scratch each cycle.
- Knowledge distillation: after each round the ensemble is fine-tuned on
  private data augmented with binarised global soft labels (`≥ 0.5 → 1`).
- Bundled public dataset (`public_dataset_seed0.npz`) — synthetic 26-feature
  array (seed 0) shared by all participants to ensure protocol compatibility.
- `FLProtocol` HTTP transport: wraps `/federated/register`,
  `/federated/update`, `/federated/global-model`, and
  `/federated/server-public-key` endpoints of the `FederatedAggregationServer`.
- Context manager support (`with FLClient(...) as client:`) on both
  `FLClient` and `FLProtocol` for safe HTTP connection cleanup.
- CLI entry point (`python -m ledgerlens_fl_client`) with `--server-url`,
  `--api-key`, `--data-dir`, `--operator-id`, `--rounds`, `--dp-epsilon`,
  `--dp-delta`, `--gradient-clip-threshold`, and `--noise-multiplier` flags;
  all parameters also settable via `FL_*` environment variables for
  Docker-native deployments.
- `Dockerfile` for containerised participant deployments.
- Comprehensive test suite: 13 passing tests covering unit behaviour,
  DP noise, Ed25519 signing, and end-to-end integration against a
  local `FederatedAggregationServer` stub.
- Package metadata: `pyproject.toml` with `hatchling` build backend,
  pinned production dependencies (`numpy`, `pandas`, `scikit-learn`,
  `xgboost`, `lightgbm`, `cryptography`, `httpx`), and `[test]` extras.

### Fixed

- Resolved 118 ruff lint errors (F401 unused imports, F821 undefined names,
  E741 ambiguous variable names) introduced when the package was merged into
  the monorepo main branch (2026-06-27, commit `bd4ddc2`).

---

[Unreleased]: https://github.com/Ledger-Lenz/Ledgerlens-core/compare/ledgerlens-fl-client-v0.1.0...HEAD
[0.1.0]: https://github.com/Ledger-Lenz/Ledgerlens-core/commits/main/packages/ledgerlens-fl-client
