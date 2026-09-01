# detection/

This package is the core of LedgerLens: it ingests trade feature vectors and produces the **LedgerLens Risk Score (0–100)** for every wallet and asset pair. It implements Benford's Law anomaly detection, graph-based wash-ring discovery, an ensemble ML pipeline (RF / XGBoost / LightGBM), a temporal sequence encoder, GNN ring classification, SHAP and causal explainability, conformal uncertainty quantification, zero-knowledge range proofs, federated learning, governance/dispute handling, compliance reporting, and all infrastructure required to publish scores on-chain via Soroban.

The files below are grouped by concern. For deeper treatment of any subsystem, follow the linked documentation pages.

---

## Detection algorithms

| File | Description |
|------|-------------|
| `benford_engine.py` | Benford's Law feature computation — chi-square, per-digit Z-score, and MAD across five rolling windows (1h, 4h, 24h, 7d, 30d) |
| `benford_baseline.py` | Baseline Benford distribution constants and helpers shared by the engine and tests |
| `graph_engine.py` | Directed trade graph construction, iterative Tarjan SCC wash-ring discovery, and CSR sparse-matrix fallback for large graphs |
| `graph_sharding.py` | Adaptive sharded graph engine — Louvain community partitioning with a multiprocessing pool for graphs that exceed `MAX_GRAPH_NODES` |
| `path_cycle_detector.py` | Cycle detection in path-payment graphs (detects circular routing across Stellar path payments) |
| `path_payment_engine.py` | Analysis of Stellar path-payment operations as potential wash-trade vectors |
| `cross_pair_engine.py` | Cross-pair activity counting, synchrony scoring, burst overlap, and shared-wallet cluster features |
| `cross_chain_linker.py` | Bayesian hypothesis engine that links Stellar wallets to EVM counterparts via Allbridge bridge events |
| `cross_chain_correlator.py` | Scores cross-chain trade correlation to detect round-trip wash patterns spanning Stellar and EVM chains |
| `amm_engine.py` | AMM pool wash-trading detection (pool imbalance, self-swap patterns) |
| `sandwich_engine.py` | Sandwich attack detection for DEX order flow |
| `temporal_patterns.py` | Temporal pattern features — inter-trade intervals, alternating buy/sell sequences, and burst-pause cycles |
| `temporal_model.py` | LSTM / Transformer sequence encoder that processes up to 200 ordered trades per wallet; fused with the tabular ensemble via learned weight `w_seq` |
| `temporal_dataset.py` | Dataset builder for temporal model training (wallet trade sequences with labels) |
| `streaming_features.py` | Online incremental feature computation for real-time scoring without full recomputation |
| `rolling_window.py` | Rolling window aggregation primitives used by the Benford engine and streaming features |

→ See [`../docs/benford_analysis.md`](../docs/benford_analysis.md), [`../docs/temporal_model.md`](../docs/temporal_model.md), [`../docs/cross_chain_detection.md`](../docs/cross_chain_detection.md), [`../docs/performance.md`](../docs/performance.md)

---

## ML pipeline

| File | Description |
|------|-------------|
| `feature_engineering.py` | Full 35-feature extraction pipeline covering Benford, trade pattern, volume/timing, wallet graph, and cross-pair feature groups |
| `dataset.py` | Labelled feature dataset builder — joins feature vectors with wash/clean labels for training |
| `model_training.py` | Trains the RF / XGBoost / LightGBM ensemble with SMOTE class-imbalance handling; saves versioned `.joblib` artefacts |
| `model_inference.py` | Real-time risk scoring — loads the active model version and runs batch inference |
| `model_registry.py` | Model version registry; tracks training metadata and controls promotion of new versions |
| `model_signing.py` | Cryptographic signing and signature verification for model artefacts (prevents tampered-model loading) |
| `model_card.py` | Generates model card metadata (training data, evaluation metrics, intended use) |
| `ensemble_reweighter.py` | Adjusts per-model ensemble weights at inference time |
| `adaptive_reweighter.py` | Dynamically reweights ensemble models based on analyst feedback signals |
| `shadow_scoring.py` | Shadow-mode scoring — runs a candidate model alongside the live model to compare outputs safely before promotion |
| `drift_monitor.py` | PSI-based feature drift detection; triggers continuous retraining when drift exceeds the configured threshold |
| `drift_detectors.py` | Pluggable drift detector implementations (PSI, Kolmogorov-Smirnov, and others) |
| `shap_drift_monitor.py` | Monitors SHAP value distributions for explanation drift, independent of raw feature drift |
| `mlflow_tracker.py` | MLflow experiment tracking integration — logs parameters, metrics, and artefacts for every training run |

---

## GNN and embeddings

| File | Description |
|------|-------------|
| `gnn_ring_detector.py` | Graph neural network classifier that scores wash-trading ring membership directly from the trade graph |
| `gnn_model.py` | GNN model architecture (message-passing layers and readout head) |
| `embedding_store.py` | SQLite-backed store for GNN wallet embeddings with model version and timestamp metadata |
| `vector_index.py` | FAISS approximate nearest-neighbour index for global similarity search across all stored wallet embeddings |

→ See [`../docs/gnn_ring_detection.md`](../docs/gnn_ring_detection.md)

---

## Explainability and causality

| File | Description |
|------|-------------|
| `shap_explainer.py` | SHAP interpretability layer — per-score feature contributions served via `GET /scores/{wallet}/explain` |
| `causal_engine.py` | DoWhy structural causal model; computes Average Treatment Effects (ATEs) via do-calculus and answers counterfactual queries |
| `counterfactual_engine.py` | Generates counterfactual risk scores under specified feature overrides |
| `counterfactual_constraints.py` | Constraint definitions that keep counterfactuals actionable and domain-valid |
| `counterfactual_translator.py` | Converts counterfactual feature deltas into plain-language remediation advice |
| `adversarial_attack.py` | Adversarial feature attack strategies (Benford camouflage, timing jitter, graph fragmentation, cross-pair rotation) |
| `adversarial_features.py` | Feature-level adversarial perturbation generation for robustness testing |
| `robustness_eval.py` | Adversarial robustness evaluation — measures score degradation under attack and reports certificate bounds |
| `conformal.py` | Conformal prediction calibration; populates `score_lower`, `score_upper`, and `prediction_set` fields on `RiskScore` |

→ See [`../docs/shap_explanation.md`](../docs/shap_explanation.md), [`../docs/causal_inference.md`](../docs/causal_inference.md), [`../docs/adversarial_robustness.md`](../docs/adversarial_robustness.md), [`../docs/adversarial_testing.md`](../docs/adversarial_testing.md), [`../docs/uncertainty_quantification.md`](../docs/uncertainty_quantification.md)

---

## Scoring and storage

| File | Description |
|------|-------------|
| `risk_score.py` | Shared `RiskScore` Pydantic schema and Benford + ML score blending logic (`RiskScore.combine`) |
| `storage.py` | SQLite-backed local `RiskScore` store — read/write interface used by the pipeline and local API |
| `feature_store.py` | Hot-tier SQLite store for feature distribution snapshots used by drift detection (last 30 days) |

> Cold-tier archival to Parquet is handled by the `DualTierFeatureStore` — see [`../docs/feature_store_archival.md`](../docs/feature_store_archival.md) and [`../docs/feature_store.md`](../docs/feature_store.md).

---

## Governance, compliance, and alerts

| File | Description |
|------|-------------|
| `governance.py` | Dispute submission, committee-vote resolution, and on-chain score-override logic |
| `dispute_store.py` | SQLite store for open and resolved disputes |
| `compliance_report.py` | Generates structured compliance reports from `RiskScore` records |
| `compliance_exporter.py` | Exports compliance data in regulator-facing formats |
| `sar_narrative.py` | Generates SAR (Suspicious Activity Report) narrative text for flagged wallets |
| `alert_engine.py` | Evaluates `RiskScore` records against subscriber thresholds and enqueues webhook alerts |
| `suppressions.py` | Alert suppression rules — silences alerts matching configured criteria (e.g. known market makers) |
| `wallet_override_store.py` | Stores manual risk score overrides for specific wallets |
| `analyst_store.py` | Analyst review queue and case management (claim/release/verdict workflow) |
| `feedback_store.py` | Stores analyst label corrections for the active learning loop |
| `api_key_store.py` | Manages API key issuance, lookup, and revocation |
| `rate_limiter.py` | Per-key and per-IP rate limiting for the local API |

→ See [`../docs/governance_protocol.md`](../docs/governance_protocol.md)

---

## Zero-knowledge proofs

| File | Description |
|------|-------------|
| `zk_prover.py` | Pedersen Sigma-Protocol ZK prover — proves a score meets a threshold without revealing the score value |
| `zk_commitment.py` | ZK commitment scheme utilities shared by the prover and verifier |
| `zk_snark_prover.py` | Groth16 zk-SNARK prover — constant-size (~256 byte) proofs with cheaper on-chain pairing verification |
| `zk_verifier.py` | ZK verifier stub — on-chain verification logic interface |

→ See [`../docs/zk_proofs.md`](../docs/zk_proofs.md), [`../docs/zk_snark_range_proof.md`](../docs/zk_snark_range_proof.md)

---

## Infrastructure and publishing

| File | Description |
|------|-------------|
| `soroban_publisher.py` | Submits `RiskScore` records on-chain via the `ledgerlens-score` Soroban contract with circuit-breaker and retry logic |
| `soroban_lease.py` | Manages Soroban storage entry lease renewals to prevent on-chain data expiry |
| `event_bus.py` | Event bus integration (Kafka / NATS) for publishing score events to downstream consumers |
| `oracle_coordinator.py` | Oracle quorum coordination — collects independent score attestations before on-chain submission |
| `oracle_node.py` | Individual oracle node logic (score attestation signing) |
| `lineage.py` | OpenLineage START / COMPLETE / FAIL event emission for ingestion, feature engineering, and training stages |
| `tracing.py` | OpenTelemetry span instrumentation for pipeline stages and Soroban submissions |
| `webhook_registry.py` | Persistent subscriber registry (URL, HMAC secret, threshold, filters) |
| `webhook_queue.py` | Durable delivery queue for pending webhook payloads with exponential-backoff retry |
| `webhook_worker.py` | Long-running worker that drains the webhook queue and delivers signed payloads |
| `exceptions.py` | Shared exception hierarchy (`SorobanSubmissionError`, `SorobanCircuitOpenError`, etc.) |

→ See [`../docs/api/detection.md`](../docs/api/detection.md)

---

## Subfolders

### `federated/` — Privacy-preserving federated learning

Enables cross-deployment model training without sharing raw trade data. The server aggregates encrypted gradient updates using Krum Byzantine-resilient aggregation and differential privacy noise injection.

| File | Description |
|------|-------------|
| `server.py` | Federated aggregation server — receives client updates, runs Krum filtering, applies DP noise, and broadcasts the new global model |
| `client.py` | Federated learning client — trains a local model update on private data and submits a signed gradient |
| `fl_model.py` | Shared model definition used by both server and clients |
| `krum.py` | Krum Byzantine-resilient aggregation algorithm implementation |
| `privacy_utils.py` | Differential privacy utilities (Gaussian mechanism, privacy budget accounting) |
| `admission.py` | Participant admission control — verifies client identity and enforces minimum data-quality requirements |
| `smpc.py` | Secure multi-party computation helpers for privacy-preserving aggregation |
| `audit.py` | Per-round audit log of aggregation decisions and participant contributions |
| `weighting.py` | Participant contribution weighting strategies (uniform, reputation-based) |

→ See [`../docs/federated_learning.md`](../docs/federated_learning.md)

---

### `red_team/` — Automated adversarial robustness evaluation

Runs automated red-team campaigns to measure how effectively adversarial wallets can evade detection.

| File | Description |
|------|-------------|
| `runner.py` | Orchestrates red-team campaigns — configures attack budgets, iterates strategies, and collects evasion metrics |
| `attacker.py` | Implements concrete attack strategies (Benford camouflage, timing jitter, graph fragmentation, cross-pair rotation) |
| `evasion_logger.py` | Logs per-attempt evasion outcomes for post-campaign analysis |

→ See [`../docs/adversarial_testing.md`](../docs/adversarial_testing.md), [`../docs/adversarial_robustness.md`](../docs/adversarial_robustness.md)

---

### `templates/` — Jinja2 report templates

| File | Description |
|------|-------------|
| `report.html` | HTML template for rendered compliance and SAR reports (used by `compliance_report.py` and `sar_narrative.py`) |
