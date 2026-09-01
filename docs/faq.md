# Frequently Asked Questions

Higher-level questions a newcomer or evaluator of LedgerLens is likely to ask.
For setup and environment problems (e.g. a broken install), this page is **not**
the right place — this FAQ is about understanding *what the project is*.

Answers are grounded in the current state of the repository. Where a topic needs
more depth, follow the linked doc.

## What is LedgerLens?

LedgerLens is a hybrid on-chain fraud detection system for the Stellar
Decentralised Exchange (SDEX). It ingests trade data from the Stellar Horizon
API, scores wallets and asset pairs for wash-trading risk using Benford's Law
digit-distribution analysis combined with ensemble machine learning, and
publishes those scores through both a REST API and an on-chain Soroban contract.
See the [project overview](../README.md#overview) for the full picture.

## What is wash trading, and why does it matter?

Wash trading is simultaneously buying and selling the same asset to artificially
inflate trading volume. On a DEX it misleads traders about real liquidity, lets
token issuers game DEX-aggregator rankings, and erodes ecosystem credibility.
Blockchain data is transparent, but the volume of on-chain activity makes manual
detection impractical — which is the gap LedgerLens fills.

## What chains does LedgerLens support?

The primary target is the **Stellar DEX**, with trade data ingested from the
Horizon API and risk scores anchored on-chain via a **Soroban** contract.
LedgerLens also has **cross-chain** detection that links Stellar wallets to EVM
counterparts (Ethereum, Base, Polygon) through Allbridge bridge events to catch
wash-trade rings that route capital across the bridge. There is also a Solana
ingestion adapter. See
[Cross-Chain Detection](cross_chain_detection.md) for details.

## How is this different from a generic fraud-detection tool?

LedgerLens is purpose-built for DEX wash trading rather than general fraud. It
combines three complementary signals that a generic tool would not have together:
Benford's Law analysis of transaction amounts, an ensemble ML classifier trained
on labelled wash-trade patterns, and graph-based ring detection over the trade
graph. It is also composable — scores are published to a Soroban contract so
other Stellar protocols (AMMs, lending, aggregators) can consume them natively,
not just read a dashboard.

## Is LedgerLens production-ready?

Not fully. The detection engine — Benford analysis, the ML ensemble, graph ring
detection, SHAP explanations, and the local read-only API — is implemented and
tested. However, several roadmap items are still open, including internal Testnet
testing, Soroban contract deployment, the public rate-limited API, and mainnet
deployment. See the [Roadmap](../ROADMAP.md) and the roadmap section of the
[README](../README.md#roadmap) for what is done versus in progress.

## How accurate is the risk score? Can I trust a single number?

Each wallet and asset pair gets a **LedgerLens Risk Score (0–100)** blended from
Benford anomaly metrics and the ML ensemble. Benford signals alone are not
sufficient (legitimate market makers can also be non-Benford), which is why they
are always combined with the ML layer. Scores can also carry calibrated
uncertainty bands via conformal prediction. Treat the score as a prioritisation
signal backed by explanations, not an absolute verdict — see
[Uncertainty Quantification](uncertainty_quantification.md).

## Can I run just the detection engine without the full API or on-chain publishing?

Yes. This repo (`ledgerlens-core`) *is* the detection engine — the public API,
dashboard, and Soroban contract live in separate repos. You can train on
synthetic data with `python cli.py train` and run the pipeline with
`python run_pipeline.py`, which writes `RiskScore` records to a local SQLite
store without serving an API. On-chain submission is opt-in: run
`python cli.py score --no-submit` to skip all Soroban calls. The local API
(`python cli.py serve`) is a separate, optional step.

## Do I need a labelled dataset to get started?

No. `python cli.py train` generates a synthetic trade history with labelled
wash-trading rings (`ingestion/synthetic_data.py`) and trains the
Random Forest / XGBoost / LightGBM ensemble on it, so you can run the full
pipeline end-to-end without any external dataset. See the
[Quick Start](../README.md#quick-start) in the README.

## Why Benford's Law? Isn't that just for accounting?

Benford's Law predicts the leading-digit distribution of naturally occurring
amounts (digit 1 ≈ 30.1%, digit 9 ≈ 4.6%). Wash-trading bots often use fixed lot
sizes or round/algorithmic amounts, producing distributions that diverge from
this expectation, which makes it a useful first-pass signal on transaction
amounts. It is one input among several, combined with ML and graph features. See
[Benford Analysis](benford_analysis.md).

## How do other protocols consume LedgerLens scores?

The Soroban contract is the on-chain truth layer. It exposes
`get_score(wallet, asset_pair) -> RiskScore`, callable by any other Soroban
contract, so an AMM or lending protocol can gate suspicious wallets natively —
for example, refusing liquidity provision above a configurable risk threshold —
without an external oracle. Off-chain consumers can use the REST API or subscribe
to signed webhook alerts.

## Is LedgerLens open source? What's the license?

Yes. LedgerLens is MIT-licensed and developed as an open-source public good for
the Stellar ecosystem — the methodology, scores, and training data are intended
to be transparent and auditable. See [`LICENSE`](../LICENSE) and the
[Contributing](../README.md#contributing) section.

## How is the project organised across repos?

`ledgerlens-core` (this repo) is the detection engine. The public API,
dashboard, Soroban contracts, canonical data store, and org-wide GitHub config
each live in their own repo. See the
[LedgerLens Organization](../README.md#ledgerlens-organization) section of the
README for the full breakdown and the cross-repo data flow.

## Where do I go for more detail?

- [Home / project index](index.md)
- [REST API reference](api_reference.md)
- [Cross-Chain Detection](cross_chain_detection.md)
- [Governance Protocol](governance_protocol.md)
- [Threat Model](threat_model.md)
- [Roadmap](../ROADMAP.md)
