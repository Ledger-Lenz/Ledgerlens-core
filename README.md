# LedgerLens 🔍

[![Built on Stellar](https://img.shields.io/badge/Built%20on-Stellar-blue?logo=stellar)](https://stellar.org)
[![Soroban Smart Contracts](https://img.shields.io/badge/Smart%20Contracts-Soroban-purple)](https://soroban.stellar.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)


Hybrid on-chain fraud detection for the Stellar DEX — detecting wash trading and artificial volume using Benford's Law combined with ensemble machine learning, with risk scores anchored on Soroban.

## Overview

LedgerLens is a fraud detection system for the Stellar Decentralised Exchange (SDEX). It ingests trade data from the Stellar Horizon API, scores wallets and asset pairs for wash-trading risk using a combination of Benford's Law digit-distribution analysis and ensemble ML classifiers, and publishes those scores both via a public REST API and an on-chain Soroban contract so other protocols can consume them natively.

### The Problem

Wash trading — simultaneously buying and selling the same asset to artificially inflate trading volume — is one of the most pervasive forms of market manipulation in DeFi. Blockchain transparency means every transaction is recorded, but the sheer volume of on-chain activity makes manual detection impossible.

On DEXs, wash trading causes real harm:

- **Traders are misled** into believing an asset has genuine liquidity and market interest when it does not
- **Token issuers manipulate rankings** on DEX aggregators and data platforms by inflating 24-hour volume figures
- **Liquidity providers lose funds** by entering pools that appear active but are dominated by self-dealing activity
- **Ecosystem credibility suffers** — inflated volume metrics on the Stellar DEX undermine confidence from institutional participants, exchanges, and new users

Existing detection approaches are either manual (slow and unscalable) or rely on simple heuristics (easily gamed). No production-grade, open-source wash trading detection system exists for the Stellar DEX — LedgerLens is built to fill that gap.

### What LedgerLens Does

At a high level, it does three things:

- **🔍 Detects** — identifies wallet pairs, trading clusters, and asset pools exhibiting statistically anomalous transaction patterns consistent with wash trading, including circular trade routing, self-matching order behaviour, and artificial volume concentration
- **📊 Scores** — assigns each wallet and each trading pair a **LedgerLens Risk Score (0–100)** based on the combined output of its Benford anomaly metrics and ML classifiers, updating continuously as new ledger data is processed
- **📡 Reports** — exposes risk scores and flagged activity through a public API and lightweight dashboard, making the intelligence accessible to DEX users, protocol teams, wallet providers, and compliance integrators without requiring technical expertise

## Features

- **Benford's Law Anomaly Engine**: Chi-square, per-digit Z-score, and MAD analysis of transaction amounts across rolling time windows (1h, 4h, 24h, 7d, 30d)
- **Ensemble ML Scoring**: Random Forest, XGBoost, and LightGBM classifiers trained on labelled wash-trade patterns with SHAP interpretability
- **LedgerLens Risk Score (0–100)**: Continuously updated composite score per wallet and per trading pair
- **On-Chain Risk Registry**: Soroban smart contract exposes risk scores so AMMs, lending protocols, and aggregators can gate suspicious activity natively
- **Public REST API**: Query scores, recent alerts, and asset risk rankings
- **Lightweight Dashboard**: Web UI for risk-score visibility without requiring technical expertise
- **Open Methodology**: Scores, features, and training data are fully transparent and auditable

## Architecture

```mermaid
graph TB
    subgraph Ingestion["Layer 1: Data Ingestion"]
        HOR[Stellar Horizon API]
        STREAM[horizon_streamer.py]
        HIST[historical_loader.py]
    end

    subgraph Detection["Layer 2: Detection Engine"]
        BENF[benford_engine.py]
        FEAT[feature_engineering.py]
        TRAIN[model_training.py]
        INFER[model_inference.py]
        SHAP[shap_explainer.py]
        SCORE[LedgerLens Risk Score]
    end

    subgraph Output["Layer 3: Contract + API"]
        CONTRACT[Soroban Contract\nledgerlens-score]
        API[FastAPI REST API]
        DASH[Web Dashboard]
        WEBHOOK[Webhook Alerts]
    end

    subgraph Consumers["Ecosystem Consumers"]
        AMM[AMMs / Lending Protocols]
        AGG[DEX Aggregators]
        USERS[Traders / Issuers]
    end

    HOR --> STREAM
    HOR --> HIST
    STREAM --> FEAT
    HIST --> FEAT
    FEAT --> BENF
    FEAT --> TRAIN
    TRAIN --> INFER
    BENF --> SCORE
    INFER --> SCORE
    SCORE --> SHAP
    SCORE --> CONTRACT
    SCORE --> API
    API --> DASH
    API --> WEBHOOK
    CONTRACT -->|get_score| AMM
    CONTRACT -->|get_score| AGG
    API --> USERS
```

### Core Components

- **ingestion/horizon_streamer.py**: Real-time trade data from the Horizon API (SSE / per-ledger polling)
- **ingestion/historical_loader.py**: Bulk historical trade ingestion
- **ingestion/operations_loader.py**: Order-book event ingestion (offer create/update/cancel) from Horizon operations
- **ingestion/account_loader.py**: Account funding-source and creation-time metadata for wallet-graph features
- **ingestion/data_models.py**: Pydantic schemas for trade, asset, and order-book records
- **detection/benford_engine.py**: Benford's Law feature computation (chi-square, Z-score, MAD)
- **detection/feature_engineering.py**: On-chain ML feature extraction
- **detection/risk_score.py**: Shared `RiskScore` schema and Benford+ML score blending
- **detection/model_training.py**: Trains the Random Forest / XGBoost / LightGBM ensemble
- **detection/model_inference.py**: Real-time risk scoring
- **detection/shap_explainer.py**: SHAP-based interpretability layer

The Soroban contract, REST API, and dashboard live in the
`ledgerlens-contracts`, `ledgerlens-api`, and `ledgerlens-dashboard` repos
respectively — see [LedgerLens Organization](#ledgerlens-organization).

## Benford's Law on the Blockchain

Benford's Law predicts that the leading digit of naturally occurring transaction amounts follows a known, non-uniform distribution (digit 1 ≈ 30.1%, digit 9 ≈ 4.6%). Wash-trading bots tend to use fixed lot sizes or round/algorithmic amounts, producing distributions that diverge from this expectation.

| Metric | What it measures |
|---|---|
| **Chi-square statistic** | Whether the overall digit distribution deviates significantly from Benford's expected distribution |
| **Z-score (per digit)** | Whether any individual digit (1–9) appears with significantly higher or lower frequency than expected |
| **Mean Absolute Deviation (MAD)** | Composite divergence measure; values above 0.015 indicate non-conformity |

Benford signals alone are insufficient (legitimate market makers can also be non-Benford), so they are combined with the ML layer below.

## Machine Learning Layer

### Feature groups (26 features, see `detection/feature_engineering.FEATURE_NAMES`)

- **Benford features (15)**: Chi-square, Z-score, and MAD across 5 rolling windows (1h, 4h, 24h, 7d, 30d)
- **Trade pattern features (4)**: counterparty concentration ratio, round-trip trade frequency, self-matching rate, order cancellation rate
- **Volume and timing features (4)**: volume-to-unique-counterparty ratio, intra-minute clustering, off-hours activity ratio, volume spike frequency
- **Wallet graph features (3)**: funding source similarity, network centrality within the trading graph, account age at time of activity

### Models

| Model | Role |
|---|---|
| **Random Forest** | Stable baseline; handles missing features gracefully |
| **XGBoost** | Primary classifier; strongest performance on tabular on-chain data |
| **LightGBM** | High-speed inference for real-time scoring |

Models are trained with **SMOTE** for class imbalance and evaluated with **AUC-ROC**, **Precision-Recall AUC**, and **F1-score**. SHAP values provide per-score interpretability.

## Soroban Smart Contract Layer

The Soroban contract is the on-chain truth layer for LedgerLens risk scores.

### Contract Functions

- `submit_score(wallet: Address, asset_pair: Symbol, score: u32, timestamp: u64)` - Registers a computed risk score on-chain (authorised LedgerLens service account only)
- `get_score(wallet: Address, asset_pair: Symbol) -> RiskScore` - Read-only; returns the most recent risk score and timestamp for a wallet/asset pair, callable by any other Soroban contract

```rust
// Simplified Soroban interface (Rust pseudocode)
pub struct RiskScore {
    pub score: u32,          // 0–100; higher = more suspicious
    pub benford_flag: bool,  // True if Benford anomaly detected
    pub ml_flag: bool,       // True if ML classifier flagged
    pub timestamp: u64,      // Ledger timestamp of last update
    pub confidence: u32,     // Model confidence 0–100
}
```

This composability lets AMMs, lending protocols, and DEX aggregators on Stellar query LedgerLens scores natively — for example, gating liquidity provision from wallets above a configurable risk threshold — without an external oracle.

## Repository Structure

This repository (`ledgerlens-core`) contains only the detection engine. The
API, dashboard, and Soroban contract live in separate repos — see
[LedgerLens Organization](#ledgerlens-organization) below.

```
ledgerlens-core/
│
├── README.md                         ← This file
├── requirements.txt                  ← Python dependencies
├── pyproject.toml                    ← Project metadata, pytest config
├── .env.example                      ← Configuration template (incl. cross-repo keys)
├── run_pipeline.py                   ← Full detection pipeline entry point
├── cli.py                            ← `ledgerlens` CLI (generate-data, train, score, serve)
├── Dockerfile / docker-compose.yml   ← Containerized local API
│
├── config/
│   └── settings.py                   ← Environment-driven configuration
│
├── ingestion/
│   ├── horizon_streamer.py           ← Real-time trade data from Horizon API
│   ├── historical_loader.py          ← Bulk historical trade ingestion
│   ├── operations_loader.py          ← Order-book event ingestion (offer ops)
│   ├── account_loader.py             ← Account funding-source / creation-time metadata
│   ├── synthetic_data.py             ← Synthetic trade/wash-ring generator for local training
│   ├── http_client.py                ← Retrying HTTP helper for Horizon calls
│   └── data_models.py                ← Pydantic schemas for trade/asset/order-book records
│
├── detection/
│   ├── benford_engine.py             ← Benford's Law feature computation
│   ├── feature_engineering.py        ← On-chain ML feature extraction
│   ├── dataset.py                    ← Labelled feature dataset builder (training)
│   ├── model_training.py             ← Train ensemble classifiers
│   ├── model_inference.py            ← Real-time risk scoring
│   ├── shap_explainer.py             ← SHAP interpretability layer
│   ├── risk_score.py                 ← Shared `RiskScore` schema + scoring logic
│   └── storage.py                    ← SQLite-backed local RiskScore store
│
├── api/
│   └── main.py                       ← Local read-only FastAPI app serving RiskScores
│
└── tests/
    └── ...
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in the Horizon, model, and cross-repo settings described in
[LedgerLens Organization](#ledgerlens-organization).

### 3. Train on synthetic data

No labelled dataset from `ledgerlens-data` is required to get started —
`cli.py train` generates a synthetic trade history with labelled
wash-trading rings (`ingestion/synthetic_data.py`) and trains the
RF/XGBoost/LightGBM ensemble on it:

```bash
python cli.py train
```

### 4. Run the detection pipeline

```bash
python run_pipeline.py
```

This scores each wallet/asset-pair combination and writes the resulting
`RiskScore` records to the local SQLite store (`LEDGERLENS_DB_PATH`).

### 5. Serve the local API

```bash
python cli.py serve --reload
```

Exposes `/health`, `/scores`, `/scores/{wallet}`, `/alerts`, and
`/assets/risk-ranking` over the locally stored `RiskScore` records — a
stand-in for `ledgerlens-api` during local development.

> The production API, dashboard, and Soroban contract live in their
> respective repos (`ledgerlens-api`, `ledgerlens-dashboard`,
> `ledgerlens-contracts`).

### Docker

```bash
docker compose up --build
```

## CLI Reference

```bash
python cli.py generate-data   # write synthetic trades/labels to CSV
python cli.py train           # train the ensemble on synthetic data
python cli.py score           # run the pipeline against live Horizon data
python cli.py serve           # serve the local API
```

## Testing

```bash
pytest
```

Covers:
- ✅ Benford's Law feature computation
- ✅ ML feature engineering (trade pattern, volume/timing features)
- ✅ Synthetic data generation and labelled dataset building
- ✅ RiskScore combination logic and SQLite storage
- ✅ Local API and CLI
- ✅ Horizon HTTP retry/backoff behaviour

## Roadmap

### Phase 1 — Foundation *(Months 1–2)*
- [x] Stellar Horizon API ingestion pipeline (historical + streaming)
- [x] Benford's Law engine for on-chain transaction amounts
- [x] Initial feature engineering from SDEX trade data
- [x] Baseline ML model training on synthetic wash trade patterns
- [ ] Internal testing on Stellar Testnet

### Phase 2 — Core Product *(Months 3–4)*
- [x] Full ensemble model training and evaluation
- [x] SHAP interpretability integration
- [ ] Soroban smart contract deployment on Testnet
- [x] Local REST API (v1, read-only) — see `api/main.py`
- [ ] Public REST API with rate limiting (`ledgerlens-api`)
- [ ] Web dashboard (beta)

### Phase 3 — Ecosystem Integration *(Months 5–6)*
- [ ] Mainnet deployment
- [ ] SDK for protocol integrations (Python + JavaScript)
- [ ] Webhook alert system for asset issuers and protocol teams
- [ ] Open dataset release: labelled SDEX wash trade patterns
- [ ] Community feedback and model refinement cycle

### Phase 4 — Scale *(Post-Grant)*
- [ ] Continuous model retraining pipeline
- [ ] Coverage expansion to AMM pools and cross-asset paths
- [ ] Integration partnerships with Stellar DEX aggregators
- [ ] Developer documentation portal

## Why This Matters for the Stellar Ecosystem

A DEX where volume figures can't be trusted is one that institutional participants and serious traders avoid. LedgerLens addresses this directly:

- **For traders** — Risk scores show which assets have genuine liquidity, without requiring on-chain expertise
- **For asset issuers** — A low risk score is a credibility signal for listings and investor materials
- **For protocol teams** — Integrate LedgerLens scores into AMM/lending contract logic to protect users from wash-traded assets
- **For the Stellar Foundation and ecosystem** — An open, verifiable, community-maintained fraud detection layer strengthens Stellar's case as trustworthy financial infrastructure

LedgerLens is an **open-source public good** — methodology, scores, and training data are transparent and auditable, and the project will always be free to query.

## Dependencies

- Python 3.10+ (`requirements.txt`)
- `soroban-sdk` — for the on-chain risk registry contract
- FastAPI, scikit-learn, XGBoost, LightGBM, SHAP

## License

MIT

## Contributing

LedgerLens is being developed as an open-source contribution to the Stellar ecosystem, submitted as part of the **Drip Wave builder programme**. We are actively looking for collaborators with experience in:

- Stellar / Soroban smart contract development (Rust)
- Python backend development and ML pipeline engineering
- On-chain data analysis and blockchain forensics
- Frontend development (dashboard)
- DeFi protocol integration

Quick checklist for contributions:
- All tests pass: `pytest`
- Code follows project style guidelines
- New features include tests
- Documentation is updated

## LedgerLens Organization

This repo is one of six in the LedgerLens organization. If a change here
touches a shared contract (below), call it out so the matching repo can be
updated.

| Repo | Role | Primary language |
|---|---|---|
| **`.github`** | Org-wide GitHub config: shared workflows, issue/PR templates, CODEOWNERS, reusable CI actions | YAML |
| **`ledgerlens-data`** | Canonical storage for raw + processed trade data and labelled training datasets used by `core` for model training | SQL / Python |
| **`ledgerlens-core`** *(this repo)* | Detection engine: Horizon ingestion, Benford's Law analysis, ML feature engineering, ensemble training/inference, SHAP explanations, `RiskScore` computation | Python |
| **`ledgerlens-api`** | Public REST API (FastAPI). Serves `RiskScore` records produced by `core`, exposes `/score`, `/alerts`, `/assets/risk-ranking`, and forwards confirmed scores to `ledgerlens-contracts` | Python (FastAPI) |
| **`ledgerlens-dashboard`** | Web dashboard consuming `ledgerlens-api`. Visualizes risk scores, SHAP explanations, and asset risk rankings | TypeScript / React |
| **`ledgerlens-contracts`** | Soroban smart contract(s) — the on-chain risk registry (`ledgerlens-score`). Exposes `submit_score` / `get_score` for composability with other Stellar protocols | Rust (Soroban) |

### Data Flow

```
ledgerlens-data  ──(labelled datasets)──▶  ledgerlens-core
                                              │
                  Horizon API ──(trades)──▶  │  (ingestion + detection)
                                              │
                                              ▼
                                    RiskScore records
                                              │
                       ┌──────────────────────┴──────────────────────┐
                       ▼                                              ▼
              ledgerlens-api (REST)                      ledgerlens-contracts (Soroban)
                       │                                              │
                       ▼                                              ▼
              ledgerlens-dashboard                     other Stellar protocols
                                                        (AMMs, lending, aggregators)
```

1. **`ledgerlens-data`** stores raw Horizon trade history and labelled wash-trade examples. `core`'s `ingestion/historical_loader.py` reads from (or writes new snapshots to) this repo for model training.
2. **`ledgerlens-core`** (this repo) runs `run_pipeline.py`: `ingestion/` pulls trades from Horizon, `detection/feature_engineering.py` computes Benford + ML features, `detection/model_inference.py` scores with the trained ensemble, and `detection/risk_score.py` produces a `RiskScore` record.
3. **`ledgerlens-api`** receives `RiskScore` records from `core` (via a shared queue/DB or direct call — see "Open Integration Points"), exposes them over REST, and forwards scores above `RISK_SCORE_THRESHOLD` to `ledgerlens-contracts` via `submit_score`.
4. **`ledgerlens-contracts`** persists the score on-chain via the `ledgerlens-score` Soroban contract, making it queryable by any other Soroban contract via `get_score`.
5. **`ledgerlens-dashboard`** calls `ledgerlens-api` to render scores, alerts, and SHAP-based explanations.

### Shared Contracts (must stay in sync across repos)

**1. `RiskScore` schema** — defined here at `detection/risk_score.py`, mirrored by `ledgerlens-api`'s response models and `ledgerlens-contracts`'s on-chain `RiskScore` struct (`contracts/ledgerlens-score/src/lib.rs`):

```python
class RiskScore:
    wallet: str
    asset_pair: str
    score: int        # 0-100
    benford_flag: bool
    ml_flag: bool
    confidence: int    # 0-100
    timestamp: datetime
```

If you change a field name, type, or range here, update the Rust struct in `ledgerlens-contracts` and the Pydantic response models in `ledgerlens-api` in the same change set (or open a tracked follow-up in each repo).

**2. Trade / Asset schema** — defined here at `ingestion/data_models.py` (`Trade`, `Asset`, `OrderBookEvent`). `ledgerlens-data` persists records in this shape; changing field names here requires a migration note for `ledgerlens-data`.

**3. Environment variables / config keys** — `.env.example` defines the cross-repo keys:
- `LEDGERLENS_API_URL` — where `core` publishes scores
- `LEDGERLENS_SCORE_CONTRACT_ID` — the deployed Soroban contract id (also used by `ledgerlens-api` and `ledgerlens-contracts`)
- `LEDGERLENS_SERVICE_SECRET_KEY` — the Soroban service account authorized to call `submit_score` (never commit; only `core`/`api` need this)
- `RISK_SCORE_THRESHOLD` — score above which `api` pushes to the contract

**4. Soroban contract interface** — `ledgerlens-contracts` exposes:
- `submit_score(wallet: Address, asset_pair: Symbol, score: u32, timestamp: u64)`
- `get_score(wallet: Address, asset_pair: Symbol) -> RiskScore`

`core` and `api` must call `submit_score` with `score` already clamped to 0-100 (see `RiskScore.combine` in `detection/risk_score.py`).

### Open Integration Points (not yet implemented)

- How `core` hands `RiskScore` records to `api` (direct DB write, message queue, or `core` calling an `api` ingestion endpoint) — see `run_pipeline.py`.
- Where labelled training data lives in `ledgerlens-data` and its schema version — see `detection/model_training.py`.
- Order-book event ingestion (needed for `round_trip_trade_frequency`, cancellation-rate features) — see TODOs in `detection/feature_engineering.py`.

### Conventions for AI Agents

- Treat this section as the source of truth for **cross-repo** contracts. Each repo's own README covers repo-local conventions.
- When a change in this repo affects a shared contract above, call it out explicitly so the corresponding change can be made in the other repo(s).
- Keep `RiskScore` and `Trade`/`Asset` field names identical (same casing, same units) across Python (`core`, `api`), Rust (`contracts`), and TypeScript (`dashboard`) — translation layers are a common source of bugs.

## Support

For issues and questions:
- GitHub Issues: [Create an issue](https://github.com/yourusername/ledgerlens/issues)
- Stellar Discord: https://discord.gg/stellar

## References

- Benford, F. (1938) 'The law of anomalous numbers', *Proceedings of the American Philosophical Society*, 78(4), pp. 551–572.
- Al Ali, A. et al. (2023) 'A powerful predicting model for financial statement fraud based on optimized XGBoost ensemble learning technique', *Applied Sciences*, 13(4).
- Antonio, G.R. (2023) 'Numbers don't lie: Decoding financial error and fraud through Benford's law', *Journal of Entrepreneurship*.
- Nti, I.K. and Somanathan, A.R. (2024) 'A scalable RF-XGBoost framework for financial fraud mitigation', *IEEE Transactions on Computational Social Systems*, 11(2), pp. 410–422.
- Yadavalli, R. and Polisetti, R. (2025) 'Optimized financial fraud detection using SMOTE-enhanced ensemble learning with CatBoost and LightGBM', *ICVADV 2025*.
- Harea, R. and Mihailă, S. (2025) 'Benford's law: Applicability in accounting and financial anomaly detection', *Challenges of Accounting for Young Researchers*, 3(1).
- Stellar Development Foundation (2024) *Horizon API Documentation*. Available at: https://developers.stellar.org/api/horizon
- Stellar Development Foundation (2024) *Soroban Smart Contract Documentation*. Available at: https://soroban.stellar.org/docs

---

<div align="center">

**LedgerLens** — Making the Stellar ledger legible.

*Built for the Stellar ecosystem. Open source. Community owned.*

</div>
