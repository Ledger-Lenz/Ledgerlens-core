# Glossary

Core domain and project-specific terms used throughout the LedgerLens codebase and documentation.
Definitions reflect how each term is used **in this codebase specifically**, not just in the
broader industry.

---

## ATE (Average Treatment Effect)

A causal inference quantity produced by `detection/causal_engine.py`. The ATE of a feature
measures the independent causal contribution of that feature to the `RiskScore`, separate from
correlational effects. It answers questions such as "would this wallet still be flagged if its
Benford distribution were normal?" See [docs/causal_inference.md](causal_inference.md).

## Benford's Law

A statistical law stating that the leading digit of naturally occurring numbers follows a
predictable, non-uniform distribution (digit 1 appears ~30.1% of the time, digit 9 ~4.6%). In
LedgerLens, the Benford engine (`detection/benford_engine.py`) applies chi-square, per-digit
Z-score, and MAD tests to trade-amount leading digits to detect wash-trading bots that use
fixed or algorithmic lot sizes, which produce digit distributions that diverge from the expected
pattern.

## Byzantine Resilience

The property of the federated learning aggregation server that allows it to produce a correct
global model update even when a minority of participating clients submit malicious or corrupted
gradient updates. LedgerLens uses the **Krum** aggregation algorithm to achieve this. See
[docs/byzantine_resilience.md](byzantine_resilience.md).

## Conformal Prediction

A statistical framework used to produce calibrated uncertainty intervals around each `RiskScore`.
Instead of a single score, the `ConformalCalibrator` produces a `score_lower` / `score_upper`
90% prediction interval and a `prediction_set` indicating which class labels are consistent with
the model's confidence. See [docs/uncertainty_quantification.md](uncertainty_quantification.md).

## Concept Drift

The phenomenon where the statistical distribution of production data shifts away from the
training distribution over time. In LedgerLens this happens when wash-trading bots adapt their
strategies to evade detection. Drift is measured using the **PSI** metric and triggers automatic
model retraining via `cli.py retrain-check`. See [docs/drift_monitor.md](drift_monitor.md).

## DEX (Decentralised Exchange)

A peer-to-peer exchange where users trade assets directly on-chain without an intermediary order
book operated by a centralised entity. LedgerLens targets the **Stellar DEX (SDEX)**, where
trades are settled atomically on the Stellar ledger.

## Federated Learning

A privacy-preserving machine learning paradigm in which model updates are computed locally by
each participant and only the gradient deltas (never raw data) are shared with a central
aggregation server. LedgerLens uses federated learning to allow multiple exchange deployments
to collaborate on model improvement without sharing their trade data. See
[docs/federated_learning.md](federated_learning.md).

## GNN (Graph Neural Network)

A class of neural network that operates directly on graph-structured data. In LedgerLens, a GNN
classifier scores wash-trading ring membership from the trade graph, complementing the
SCC-based structural detector. Wallet embeddings are stored in `detection/embedding_store.py`
and indexed for similarity search via `detection/vector_index.py`. See
[docs/gnn_ring_detection.md](gnn_ring_detection.md).

## Horizon API

The public REST API provided by the Stellar Development Foundation for querying ledger data,
trade history, account records, and order-book events on the Stellar network.
`ingestion/horizon_streamer.py` consumes the Horizon **Server-Sent Events (SSE)** stream for
real-time data; `ingestion/historical_loader.py` uses paginated Horizon calls for bulk backfill.

## LSTM (Long Short-Term Memory)

A recurrent neural network architecture that captures long-range temporal dependencies in
sequential data. LedgerLens uses an LSTM (or Transformer) encoder to process a wallet's ordered
trade history, detecting temporal patterns such as regular inter-trade intervals and
alternating buy/sell sequences. Its output is fused with the tabular ensemble score via a
learned weight `w_seq`. See [docs/temporal_model.md](temporal_model.md).

## MAD (Mean Absolute Deviation)

A composite Benford divergence metric. In LedgerLens, MAD is computed as the mean of the
absolute differences between the observed and expected Benford digit frequencies. Values above
**0.015** indicate non-conformity and contribute to the Benford flag in a `RiskScore`.

## PSI (Population Stability Index)

A statistical measure of how much a feature's distribution has shifted between two time periods
(training reference vs. recent production). PSI = 0 means identical distributions; PSI ≥ 0.20
signals significant drift that warrants model retraining. LedgerLens declares drift when at
least three features exceed the PSI threshold. See [docs/drift_monitor.md](drift_monitor.md).

## RiskScore

The primary output schema of the LedgerLens detection engine, defined in
`detection/risk_score.py`. It carries a composite **0–100 score** (higher = more suspicious),
`benford_flag`, `ml_flag`, `confidence`, timestamp, and optional conformal prediction interval
fields. It is the shared contract between `ledgerlens-core`, `ledgerlens-api`, and the Soroban
on-chain registry.

## Ring Detection

The process of identifying groups of wallets engaged in circular trading — each wallet sells to
the next in a cycle — to artificially inflate reported volume. `detection/graph_engine.py`
builds a directed trade graph and runs **Tarjan's SCC algorithm** to find Strongly Connected
Components with three or more members, treating each as a candidate wash ring.

## SAR (Suspicious Activity Report)

A regulatory filing required by financial institutions when they detect potentially illicit
activity. LedgerLens risk scores and flagged wallet activity are designed to serve as supporting
evidence for SAR filings by compliance teams, surfaced through the compliance export and analyst
review interfaces.

## SCC (Strongly Connected Component)

A subgraph in which every node is reachable from every other node by following directed edges.
In the LedgerLens trade graph, an SCC with three or more accounts indicates a potential
wash-trading ring, because every participant can reach every other participant through a chain
of trades.

## SDEX (Stellar Decentralised Exchange)

The built-in order-book exchange on the Stellar network, where any Stellar account can place
offers to buy or sell any asset pair. All SDEX trades are settled on-ledger and are accessible
via the Horizon API. LedgerLens focuses specifically on wash-trading detection within the SDEX.

## SHAP (SHapley Additive exPlanations)

A game-theoretic framework for explaining individual ML model predictions by assigning each
feature a contribution value. `detection/shap_explainer.py` computes per-score SHAP values for
each of the three ensemble models (Random Forest, XGBoost, LightGBM) and serves them via
`GET /scores/{wallet}/explain`. See [docs/shap_explanation.md](shap_explanation.md).

## SMOTE (Synthetic Minority Over-sampling Technique)

A data augmentation technique that generates synthetic training examples for the minority class
(wash-trading wallets) by interpolating between existing minority samples. LedgerLens applies
SMOTE during model training to address the class imbalance inherent in fraud detection datasets.

## Soroban

Stellar's smart contract platform, built on WebAssembly (WASM) and written in Rust. LedgerLens
deploys a `ledgerlens-score` Soroban contract that stores risk scores on-chain, making them
queryable by other Stellar protocols (AMMs, lending contracts, aggregators) via `get_score`
without an external oracle.

## Wash Trading

A form of market manipulation in which a trader (or coordinated group of traders) simultaneously
buys and sells the same asset to create artificial trading volume, mislead other market
participants about an asset's liquidity, and inflate DEX aggregator rankings. It is the primary
detection target of LedgerLens.

## ZK-SNARK (Zero-Knowledge Succinct Non-Interactive Argument of Knowledge)

A cryptographic proof system that allows one party to prove a statement (e.g. "this wallet's
risk score is below threshold 30") to another party without revealing the score itself.
LedgerLens supports a **Groth16** ZK-SNARK backend for privacy-preserving threshold proofs,
alongside a simpler Pedersen sigma-protocol default. See
[docs/zk_snark_range_proof.md](zk_snark_range_proof.md).
