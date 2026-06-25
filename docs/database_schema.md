# LedgerLens Database Schema

LedgerLens uses a single SQLite database (path: `LEDGERLENS_DB_PATH`, default `./ledgerlens.db`).
Schema migrations are tracked in the `schema_migrations` table and applied automatically at startup
via `detection.storage.init_db()`.

---

## Core Score Tables

### `risk_scores`

Stores the output of each pipeline scoring run — one row per wallet/asset-pair combination per run.

| Column         | Type       | Notes                                            |
| -------------- | ---------- | ------------------------------------------------ |
| `id`           | INTEGER PK | Auto-increment                                   |
| `wallet`       | TEXT       | Stellar account ID                               |
| `asset_pair`   | TEXT       | e.g. `XLM/USDC`                                  |
| `score`        | INTEGER    | 0–100 risk score                                 |
| `benford_flag` | INTEGER    | 1 if Benford anomaly detected                    |
| `ml_flag`      | INTEGER    | 1 if ML classifier flagged                       |
| `confidence`   | INTEGER    | Model confidence 0–100                           |
| `timestamp`    | TEXT       | ISO-8601 scoring timestamp                       |
| `shap_json`    | TEXT       | JSON array of `{feature, shap_value}` (nullable) |

Indexes: `wallet`, `asset_pair`

---

### `feature_vectors`

Stores raw ML feature vectors and SHAP values for each scored wallet/pair.

| Column          | Type       | Notes                                       |
| --------------- | ---------- | ------------------------------------------- |
| `id`            | INTEGER PK |                                             |
| `wallet`        | TEXT       |                                             |
| `asset_pair`    | TEXT       |                                             |
| `features_json` | TEXT       | JSON object: `{feature_name: value, ...}`   |
| `shap_json`     | TEXT       | JSON array of SHAP contributions (nullable) |
| `timestamp`     | TEXT       |                                             |

Indexes: `wallet`, `asset_pair`

---

## Causal Inference

### `causal_ate_cache`

Caches the fitted Average Treatment Effect (ATE) table per model version.
API requests read from this table rather than re-fitting the structural equations on every call.

| Column          | Type      | Notes                                                    |
| --------------- | --------- | -------------------------------------------------------- |
| `model_version` | TEXT      | Version tag (set via `LEDGERLENS_MODEL_VERSION` env var) |
| `feature_name`  | TEXT      | Observable feature name (e.g. `wash_ring_membership`)    |
| `ate`           | REAL      | Average Treatment Effect in risk-score units             |
| `computed_at`   | TIMESTAMP | When the ATE was computed                                |

Primary key: `(model_version, feature_name)`

**Cache invalidation**: update `LEDGERLENS_MODEL_VERSION` after retraining, or call
`CausalEngine.invalidate_cache()` programmatically.

**Security**: the ATE cache is read-only from the API. No runtime DAG modification is
possible — the causal structure is hardcoded and not user-configurable.

---

## On-Chain Audit

### `on_chain_submissions`

Audit log for every Soroban contract submission attempt.

| Column          | Type       | Notes                                          |
| --------------- | ---------- | ---------------------------------------------- |
| `id`            | INTEGER PK |                                                |
| `wallet`        | TEXT       |                                                |
| `asset_pair`    | TEXT       |                                                |
| `score`         | INTEGER    |                                                |
| `tx_hash`       | TEXT       | Soroban transaction hash (nullable on failure) |
| `status`        | TEXT       | `success`, `failed`, `skipped`                 |
| `error_message` | TEXT       | Nullable; reason for failure                   |
| `submitted_at`  | TEXT       | ISO-8601 timestamp                             |

Indexes: `wallet`, `status`

---

## Graph Detection

### `wash_rings`

Detected wash-trading ring clusters from Tarjan SCC analysis.

| Column             | Type       | Notes                                   |
| ------------------ | ---------- | --------------------------------------- |
| `id`               | INTEGER PK |                                         |
| `accounts_json`    | TEXT       | JSON array of Stellar account IDs       |
| `total_volume`     | REAL       | Aggregate edge volume within the SCC    |
| `cycle_volume`     | REAL       | Best bottleneck cycle volume            |
| `avg_trade_count`  | REAL       | Mean trade count per edge in the ring   |
| `timing_tightness` | REAL       | Std. dev. of trade timestamps (seconds) |
| `truncated`        | INTEGER    | 1 if the SCC exceeded `max_ring_size`   |
| `detected_at`      | TEXT       | ISO-8601 detection timestamp            |

Index: `detected_at`

---

## AMM and Path Payments

### `liquidity_pool_trades`

AMM pool trade events from Stellar's liquidity pool operations.

| Column               | Type       | Notes                      |
| -------------------- | ---------- | -------------------------- |
| `id`                 | INTEGER PK |                            |
| `trade_id`           | TEXT       | Horizon operation ID       |
| `pool_id`            | TEXT       | Stellar liquidity pool ID  |
| `base_account`       | TEXT       |                            |
| `base_asset_pair`    | TEXT       |                            |
| `counter_asset_pair` | TEXT       |                            |
| `base_amount`        | REAL       |                            |
| `counter_amount`     | REAL       |                            |
| `base_is_seller`     | INTEGER    | 1 = base account is seller |
| `timestamp`          | TEXT       |                            |

Indexes: `pool_id`, `base_account`

### `path_payments`

Path payment operations (multi-hop cross-asset swaps).

| Column                   | Type       | Notes                         |
| ------------------------ | ---------- | ----------------------------- |
| `id`                     | INTEGER PK |                               |
| `payment_id`             | TEXT       |                               |
| `transaction_hash`       | TEXT       |                               |
| `source_account`         | TEXT       |                               |
| `destination_account`    | TEXT       |                               |
| `source_asset_pair`      | TEXT       |                               |
| `destination_asset_pair` | TEXT       |                               |
| `source_amount`          | REAL       |                               |
| `destination_amount`     | REAL       |                               |
| `hop_count`              | INTEGER    | Number of intermediate assets |
| `strict_send`            | INTEGER    | 1 = strict-send payment       |
| `timestamp`              | TEXT       |                               |

Indexes: `source_account`, `transaction_hash`

### `circular_path_routes`

Atomic circular route detections (source asset == destination asset cycles).

| Column                   | Type       | Notes                                      |
| ------------------------ | ---------- | ------------------------------------------ |
| `id`                     | INTEGER PK |                                            |
| `transaction_hash`       | TEXT       |                                            |
| `accounts_json`          | TEXT       | JSON array of accounts in the cycle        |
| `hop_count`              | INTEGER    |                                            |
| `cycle_volume`           | REAL       | Volume of the cycle                        |
| `is_atomic_self_payment` | INTEGER    | 1 if source == destination account         |
| `touches_pool`           | INTEGER    | 1 if any hop goes through a liquidity pool |
| `timestamp`              | TEXT       |                                            |

Index: `transaction_hash`

---

## Cross-Chain

### `bridge_transfers`

EVM ↔ Stellar bridge transfer events detected by the cross-chain linker.

| Column            | Type       | Notes                                              |
| ----------------- | ---------- | -------------------------------------------------- |
| `id`              | INTEGER PK |                                                    |
| `chain`           | TEXT       | `ethereum`, `base`, `polygon`                      |
| `direction`       | TEXT       | `stellar_to_evm` or `evm_to_stellar`               |
| `evm_wallet`      | TEXT       | EVM address (EIP-55 checksum)                      |
| `stellar_wallet`  | TEXT       | Stellar account ID                                 |
| `amount_usd`      | REAL       | Estimated USD value (nullable; may be manipulated) |
| `token`           | TEXT       | Token symbol                                       |
| `tx_hash_evm`     | TEXT       | EVM transaction hash                               |
| `tx_hash_stellar` | TEXT       | Stellar transaction hash (nullable)                |
| `timestamp`       | TEXT       |                                                    |

Indexes: `stellar_wallet`, `evm_wallet`, `timestamp`

---

## Model Governance

### `drift_reports`

Feature distribution drift reports from the retrain-check pipeline.

| Column                 | Type       | Notes                                |
| ---------------------- | ---------- | ------------------------------------ |
| `id`                   | INTEGER PK |                                      |
| `triggered_at`         | TEXT       |                                      |
| `drift_detected`       | INTEGER    | 1 = drift threshold exceeded         |
| `psi_report_json`      | TEXT       | JSON: per-feature PSI values         |
| `psi_threshold`        | REAL       | Threshold used                       |
| `min_drifted_features` | INTEGER    | Required drifted features to trigger |

Index: `triggered_at`

### `retrain_runs`

Per-model retraining outcome records.

| Column            | Type       | Notes                                  |
| ----------------- | ---------- | -------------------------------------- |
| `id`              | INTEGER PK |                                        |
| `triggered_at`    | TEXT       |                                        |
| `drift_report_id` | INTEGER    | FK → `drift_reports.id` (nullable)     |
| `model_name`      | TEXT       | `random_forest`, `xgboost`, `lightgbm` |
| `old_version`     | TEXT       | Model version before retrain           |
| `new_version`     | TEXT       | Model version after retrain            |
| `old_auc_roc`     | REAL       |                                        |
| `new_auc_roc`     | REAL       |                                        |
| `promoted`        | INTEGER    | 1 if the new model was promoted        |
| `forced`          | INTEGER    | 1 if retrain was manually forced       |

Indexes: `triggered_at`, `model_name`

### `robustness_reports`

Adversarial robustness evaluation results.

| Column             | Type       | Notes                                 |
| ------------------ | ---------- | ------------------------------------- |
| `id`               | INTEGER PK |                                       |
| `created_at`       | TEXT       |                                       |
| `model_version`    | TEXT       |                                       |
| `asr_json`         | TEXT       | JSON: attack success rate per method  |
| `mean_map`         | REAL       | Mean minimum adversarial perturbation |
| `p95_map`          | REAL       | 95th-percentile MAP                   |
| `certified_radius` | REAL       | Randomized smoothing certified radius |
| `n_samples`        | INTEGER    |                                       |
| `epsilon`          | REAL       | Perturbation budget                   |
| `report_json`      | TEXT       | Full report (nullable)                |

Index: `created_at`

---

## Dispute and Governance

### `score_disputes`

Analyst-submitted dispute records for published risk scores.

| Column                 | Type       | Notes                                    |
| ---------------------- | ---------- | ---------------------------------------- |
| `id`                   | INTEGER PK |                                          |
| `dispute_id`           | TEXT       | UUID                                     |
| `wallet`               | TEXT       |                                          |
| `asset_pair`           | TEXT       |                                          |
| `disputed_score`       | INTEGER    |                                          |
| `soroban_tx_hash`      | TEXT       | On-chain score submission being disputed |
| `evidence_url`         | TEXT       | HTTPS URL to evidence (nullable)         |
| `submitted_at`         | TEXT       |                                          |
| `status`               | TEXT       | `pending`, `approved`, `rejected`        |
| `committee_votes_json` | TEXT       | JSON array of vote records               |
| `resolved_at`          | TEXT       | Nullable                                 |
| `resolution`           | TEXT       | Nullable                                 |

Indexes: `dispute_id`, `wallet`

### `score_overrides`

Records when a dispute resolution overrides a published score on-chain.

| Column        | Type       | Notes                               |
| ------------- | ---------- | ----------------------------------- |
| `id`          | INTEGER PK |                                     |
| `wallet`      | TEXT       |                                     |
| `asset_pair`  | TEXT       |                                     |
| `dispute_id`  | TEXT       |                                     |
| `tx_hash`     | TEXT       | Soroban override tx hash (nullable) |
| `status`      | TEXT       |                                     |
| `recorded_at` | TEXT       |                                     |

Index: `dispute_id`

### `runtime_config`

Key-value store for runtime configuration overrides (e.g. `risk_score_threshold`).

| Column       | Type    | Notes                 |
| ------------ | ------- | --------------------- |
| `key`        | TEXT PK | Config key            |
| `value`      | TEXT    | Config value (string) |
| `updated_at` | TEXT    |                       |

### `governance_proposals`

Governance proposals for runtime parameter and committee changes.

| Column                 | Type       | Notes                                        |
| ---------------------- | ---------- | -------------------------------------------- |
| `id`                   | INTEGER PK |                                              |
| `proposal_id`          | TEXT       | UUID                                         |
| `proposal_type`        | TEXT       | e.g. `risk_score_threshold`, `committee_add` |
| `proposed_value`       | TEXT       |                                              |
| `proposed_by_key_hash` | TEXT       | SHA-256 of proposer's public key             |
| `votes_for_json`       | TEXT       | JSON array of voter key hashes               |
| `votes_against_json`   | TEXT       | JSON array                                   |
| `status`               | TEXT       | `open`, `passed`, `rejected`, `expired`      |
| `created_at`           | TEXT       |                                              |
| `expires_at`           | TEXT       |                                              |

Index: `proposal_id`

### `committee_members`

Active committee members authorised to vote on disputes and governance proposals.

| Column           | Type       | Notes                           |
| ---------------- | ---------- | ------------------------------- |
| `id`             | INTEGER PK |                                 |
| `public_key_hex` | TEXT       | Raw public key (hex)            |
| `key_hash`       | TEXT       | SHA-256 hash used in audit logs |
| `added_at`       | TEXT       |                                 |

Index: `key_hash`

---

## Alerts

### `alerts`

Typed manipulation alert records (e.g. `SANDWICH_ATTACK`, `CIRCULAR_ROUTE`).

| Column        | Type       | Notes                                |
| ------------- | ---------- | ------------------------------------ |
| `id`          | INTEGER PK |                                      |
| `alert_type`  | TEXT       | One of `AlertType` enum values       |
| `wallet`      | TEXT       |                                      |
| `asset_pair`  | TEXT       |                                      |
| `pool_id`     | TEXT       | Nullable; for AMM-related alerts     |
| `detail_json` | TEXT       | JSON blob with alert-specific fields |
| `timestamp`   | TEXT       |                                      |

---

## Streaming Feature Store

### `wallet_feature_states`

Cold-layer cache for the streaming feature store (hot layer is Redis).

| Column         | Type       | Notes                                            |
| -------------- | ---------- | ------------------------------------------------ |
| `id`           | INTEGER PK |                                                  |
| `wallet`       | TEXT       |                                                  |
| `asset_pair`   | TEXT       |                                                  |
| `state_json`   | TEXT       | JSON: current feature state for this wallet/pair |
| `last_updated` | TEXT       |                                                  |

Unique index: `(wallet, asset_pair)`
Index: `last_updated`

---

## Pair Correlations

### `pair_correlations`

Cross-asset-pair correlation records from the multi-pair synchrony analysis.

| Column                | Type       | Notes                                  |
| --------------------- | ---------- | -------------------------------------- |
| `id`                  | INTEGER PK |                                        |
| `pair_a`              | TEXT       |                                        |
| `pair_b`              | TEXT       |                                        |
| `correlation_r`       | REAL       | Spearman correlation coefficient       |
| `method`              | TEXT       | e.g. `spearman`                        |
| `shared_wallet_count` | INTEGER    | Number of wallets active on both pairs |
| `timestamp`           | TEXT       |                                        |

Indexes: `pair_a`, `pair_b`

---

## Schema Migrations

### `schema_migrations`

Tracks applied database migrations to prevent double-application.

| Column        | Type       | Notes                    |
| ------------- | ---------- | ------------------------ |
| `version`     | INTEGER PK | Migration version number |
| `description` | TEXT       | Short description        |
| `applied_at`  | TEXT       | ISO-8601 timestamp       |
| `status`      | TEXT       | `applied` or `failed`    |
