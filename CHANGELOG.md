# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/google-github-actions/release-please-action);
merging a release PR (created by the `release-please` GitHub Action) tags the
commit, generates this file, and publishes a tagged Docker image to GHCR.

## 0.1.0 (2026-06-30)


### Features

* **#100:** add Benford baseline calibration against market-wide distributions ([c668315](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c668315c3ac176d0e5910a744a40e41c2365a7cf))
* **#152:** stateful rolling-window streaming scorer ([eced102](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eced10240dd8d9d0cb229a3706f34eb834bef6e1)), closes [#152](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/152)
* **#158:** add OpenAPI 3.1 schema, route annotations, and export CLI command ([6136008](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6136008868201b38474cb33636d73cb548835f00))
* **#158:** OpenAPI 3.1 schema auto-generation and Swagger/ReDoc docs ([b58798e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b58798e9676b991fe0db0d3eee5a7ace963810df))
* **#166:** add HEALTHCHECK to non-root multi-stage Docker image ([fc4ca1f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fc4ca1fc23a343c903ef8850b06cac68aa01159c))
* **#166:** multi-stage Docker build with non-root user and minimal image ([859319c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/859319ca783071a0a46918569b5a876a7b0b4e42))
* **#168:** build Alembic-based database migration framework with rollback support ([f1f923c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f1f923c9fe9acdba799383feca59cdced7fe7169))
* **#169:** config schema validation with fail-fast startup checks ([2942c2d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2942c2dcf92aa427339a1c31b8d39768611227bd))
* **#173:** add Hypothesis property-based tests for feature engineering ([612d29d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/612d29db734d7c773208839804edd7d2a858c4cf))
* **#177:** Alert deduplication engine with SQLite-backed state ([acc7d4b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/acc7d4b0f3b4bc3e2820a08068a61e7163ad898b))
* **#177:** implement AlertDeduplicator with state machine and hysteresis ([c4aab2d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c4aab2dad7f3f6bffd15be57cc553ada26efc86a))
* **#178:** implement alert suppression rules for whitelisted accounts ([da2f05d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/da2f05d2dbcc37d8a6191ffbc82361a91f5e4a88))
* **#179:** add GET /admin/webhooks/dlq and POST /admin/webhooks/dlq/{id}/retry endpoints ([baa6169](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/baa616973a7e45f2dd0ca871d2335c62712f57a6))
* **#179:** add WebhookRetryQueue with exponential backoff and DLQ ([7b398f5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7b398f5dc43ec4aa58b7c7981a77f77421f43ccd))
* **#180:** build data retention policy engine with per-table TTL and Parquet archival ([0f8681c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0f8681c0f4c08a8c3dd59856c0c9b3ca515af7ae))
* **#181:** implement wallet allowlist/denylist with audit trail ([14a48e3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/14a48e3d405511a000f42abc96c23c7f45d05042))
* **#181:** Wallet allowlist/denylist API with audit trail ([13f58e7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/13f58e7623b98ff7c8103caeffe1527c34e8cba4))
* **#183:** add Solana SPL token trade ingestion adapter ([22cf93c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/22cf93cc9b521f9e3c7e2f66892c9be7e4434c59))
* **#183:** integrate SolanaAdapter into run_pipeline for cross-chain feature coverage ([53fae16](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/53fae1697ef77c5e5d48955fb9a6ae0d642beb83))
* **#184:** add Base and Arbitrum L2 EVM trade ingestion adapters ([263f8da](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/263f8da35d89b9d33c7915c0a8f12dfd7dcbb947))
* **#195:** add API key management with scoped permissions and per-key rate limits ([fe3e634](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fe3e6340c8c1f8e002dadd19fed2dfeeaddfb6b4))
* **#195:** API key management with scoped permissions and per-key rate limits ([d58df46](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d58df46dd8049bab23a1525a4d7724695945313f))
* **#296:** implement SSE streaming engine with Redis pub/sub ([5637c8e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5637c8eb4810d4a9753b5588fa980cb433474655)), closes [#296](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/296)
* **#297:** implement event sourcing and immutable audit log ([e8204be](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e8204becb4a0bfc67928a87788e1d543f6ca050c)), closes [#297](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/297)
* **#298:** implement temporal pattern analysis engine ([9f090bc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9f090bc8db24509d1e50926251e11e4ca190d22a)), closes [#298](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/298)
* **#91:** add account metadata enrichment pipeline with TTL cache ([6ac4743](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6ac4743f5ed31639ebdf4117bfe8749e62e73d20))
* **#92:** add order-book event replay buffer with sequence-gap detection ([1a45f14](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1a45f1436f6bd4055305a08977bf3e57d1fc2f63))
* **#94:** add trade ingestion dead-letter queue with structured error classification ([7622ffb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7622ffbe7aa0779e3f7af69fe6404caddc0c82b0))
* add 120 GitHub issue definitions for LedgerLens roadmap ([cfecf2b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cfecf2b133c3bf0c8fb4ae8b54788de984365659))
* add 5 advanced 200-point issues ([#121](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/121)-125) ([831f02f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/831f02fd40f110db58dc399fbc71d64283fec89d))
* add admin-gated model observability API endpoints ([576393d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/576393d40cb21b59c10c238e3ba29b6ae1fa3241))
* add adversarial feature defense layer ([9167782](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9167782943d2d64b91b934969c9aa24b9879f396))
* add AMM wash-trade session detection with anomaly scoring ([7dcdd1e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7dcdd1e3fd03561dd6df444643e0bd839cc16812))
* add analyst feedback store with importance-weighted active learning ([e298c53](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e298c53d33a7241f2ece44055c1a2f9a3c4a8f6b))
* add Backpressure and Flow Control to the Real-Time Trade Streamer ([ba0cec7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ba0cec7d5223654cf759bce98ed3c7a15c674451))
* add backtesting framework for model evaluation on labelled data ([3b84b8b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3b84b8b4d5d98d556e1de7b5eb09a35985edd620))
* add batch wallet scoring endpoint with async job queue ([#161](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/161)) ([8eafe11](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8eafe1177bf8ce7668fd76a221c5cec948775e21))
* add Bayesian probabilistic scoring for cross-chain wallet links ([f44294f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f44294f7a5ce80b1722c7a79b9aa5c0fff2d97b2))
* add circuit breaker for Horizon API and Redis feature store calls ([9466ff1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9466ff190592e5d57ba61d55efa9d2574322acdc))
* add ComplianceReportGenerator for self-contained audit reports ([d7f502a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d7f502ac0761a7376f35ee3e23b2e564a3ad2178))
* add conformal prediction uncertainty quantification ([9e19821](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9e19821cfb625ec3ec5f8d6df42a3a88dbe0d690))
* add continuous retraining with drift detection and model versioning ([e22e7c7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e22e7c7b0e8ca8750781c72806fcd6af5ddeabb5))
* add CSV and Parquet export endpoints ([#163](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/163)) ([f5eaf81](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f5eaf81e85fde9a5991c2127b72b3751d6085aa6))
* add deterministic TradeFactory for test data generation ([b3dad8b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b3dad8bfb4536cc7fe2172946258f659553f0f48))
* add dispute & governance system, runtime config, API endpoints, docs, and tests ([5383f6d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5383f6dfd97a80b24540cc6bdea5260d764e2df7))
* add DoWhy causal engine with do-calculus interventions ([f93c844](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f93c8443d810ee506662875abcdbd68d1226208d))
* add E2E test suite for full request-path validation ([2009b15](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2009b15f2ccaf403c874b116c8e003614b169f59))
* add ED25519 asymmetric model signing to prevent supply-chain attacks ([3e5c421](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3e5c421bc60142ddd915ba4a97f5d1b9e780f070))
* add Federated Learning framework with Knowledge Distillation FedAvg ([583ed21](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/583ed210ca02f6724ded0504d08e4e3e844f7da9))
* add graceful shutdown with request draining and readiness probe ([7eac462](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7eac462f92eb2c6b6906a0eb9f520ae71aaffef7))
* add hop_payment_cycles table and persistence for PathCycleDetector ([#121](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/121)) ([82092e1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/82092e11dbcf15fb3aaba4d1839bd39d137e2ea6))
* add KS and Kuiper statistical tests for small-sample Benford conformity ([810beb8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/810beb85fe45d8ef953ec27e4bacfe3464d3271e))
* add Kubernetes Helm chart for production deployment ([2e93b87](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2e93b87ac2fb5e1cf2acb374b452bac758948579))
* add ledgerlens-sdk Python client package ([a5318fd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a5318fd68b419f5371005d5cfeaa04a0835eb23e))
* add MLflow experiment tracking for model training runs ([ee87d89](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ee87d893a9c2459f9341f4f225283f507be87344))
* add Optuna Bayesian hyperparameter optimization for ensemble models ([da741d2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/da741d2f780ba50dde78eebf459c26211e8c51b4))
* add path_cycle_count and path_cycle_recovery_ratio ML features ([#121](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/121)) ([1e2eeda](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1e2eeda2f86cf8618b2f884d2aaa7d400e3ca815))
* add PathPaymentGraph and PathCycleDetector for 7-hop cycle detection ([#121](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/121)) ([c25b8c5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c25b8c565e14fef8ed8e4557de3ec1f602a27378))
* add per-feature PSI time-series tracking, heatmap export, and drift alerting ([470b74d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/470b74d46ca8f02883e1df4fa7c00fcafd812416))
* add per-version SHAP importance tracking and stability-gated promotion ([9e9352d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9e9352deb5552e38cf6a00ecdfe798a178199d0c))
* add persistent cursor ([6432f61](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6432f61601782f5635f755ea2428d564424ab10d))
* Add RDP differential privacy accounting to federated learning ([dd03cb6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dd03cb64202e56be91ce3761cdab3a2cb8ea58a7))
* add Real-Time Ingestion Throughput Metrics and Prometheus Export ([b1555fe](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b1555fe001ef6427333ed32cc72ece372e7e9de7))
* add score bulk CLI subcommand for bulk wallet analysis ([#164](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/164)) ([e7c4181](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e7c41816e0e285e05c8a1b44e8e09c0b7b09cc17))
* add score flag filters ([8c59635](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8c59635954c71032c99b38cef524a3e97eac7ca0))
* add scoring pipeline performance benchmark suite ([22b83b2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/22b83b2c4460a902439048904eb2c2c5216cd664))
* add shadow model scoring for safe model promotion decisions ([5ea8b71](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5ea8b71495a208cfbc13b4cd123a6d09e1b9163e))
* add shell completion script generation for CLI ([8705073](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8705073b3ab417202327bd0d895327fb486f9630))
* Add standalone federated learning client library (ledgerlens-fl-client) ([58f28a2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/58f28a22bf5abc3b190adf1421bef4af0d18fa71))
* add token-bucket rate limiter, backpressure, and adaptive rate control for Horizon SSE ingestion ([9aa61fa](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9aa61fa596bfeba630f2793d8ea80f28e0f1eee8))
* add Uniswap V3 and Curve DEX adapters for cross-chain wash-trade detection ([350ba51](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/350ba51e338760c88e815cd3c91addc86f1b78f0))
* add WashRingGNN classifier with TradeGraphDataset and ensemble fusion ([48d000c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/48d000c88f7bdbe9a2e27606e3b9503bf9f336f9))
* add WashTradeSequenceModel and TradeSequenceEncoder for temporal wash-trade detection ([bafeddf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bafeddfc17a59bf151c6a06c78400310db31cabe))
* add WebSocket push channel for real-time risk score alerts ([#162](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/162)) ([97233ab](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/97233abb764f4436e1c030ea3d8bfc374a98c518))
* adversarial evasion detection and robustness evaluation ([3786552](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/37865520c5b55b74e0e89d72246aacdcc7a9fe62))
* **api/storage:** add SQL-level limit/offset paging for latest scores and alerts ([d747bf1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d747bf1051aacad45ca1e55900735003013dc779))
* async pipeline with concurrent I/O and batched ML inference ([dab746c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dab746ca3fe3034b87932458facbecfd5cd010b4))
* **benford:** add BenfordStreamCounter for O(1) incremental Benford analysis ([8a7b579](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8a7b57932897f423881d276837f82615bb184eba))
* Build a Configurable Trade Filter Pipeline Before Detection Ingestion ([b2b7cb5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b2b7cb51c38abc5eb96c01d173782e91f991f452))
* build admin REST API for model lifecycle and system configuration ([#160](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/160)) ([26ce502](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/26ce50260acca6bd590178334b045021928d9ea0))
* Build an EVM Bridge Event Deduplication Layer for Cross-Chain Ingestion ([67fd75c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/67fd75cd574a95bd77e804d213ec9f6da833a469))
* build multi-signature oracle quorum for tamper-resistant on-chain score publication ([453c6f7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/453c6f715ee20569e300cc75308007a8a488dd00)), closes [#148](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/148)
* build parallel history trade loader ([eb71e6b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eb71e6be2e7f727ce81551e01139ee8efff4d3e3))
* build streaming feature computation engine with sub-second latency (issue [#104](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/104)) ([c15195d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c15195dd6a7e8b22886aecd6809ba06747206cea))
* chaos tests, MkDocs site, distributed tracing, analyst dashboard ([#197](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/197) [#198](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/198) [#199](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/199) [#200](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/200)) ([a3930a1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a3930a1474f76907b8a8d075ff694de6fe1298f4))
* **cli:** add score bulk subcommand for parallel wallet CSV scoring ([#164](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/164)) ([7df414b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7df414b61ecdac602d56b3dd2e6f93d31538c3c6))
* **compliance:** audit-log, rate-limit, and score-gate SAR/Travel-Rule exports ([97a4770](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/97a4770f2e5dc68bb18a2d49a7c38122340f43c8))
* Create a Synthetic Wash-Trade Scenario Generator with Configurable Attack Profiles ([91f9440](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/91f9440c4c6af1b2cbb5f578ba2cbe6de414039c))
* cross-asset correlation analysis for coordinated wash-trading detection ([ebd8a11](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ebd8a110e9d269b652948fcdde641c8784a74cbb))
* **db:** consolidate CLI db group for Alembic migration framework ([#168](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/168)) ([b9bb1ae](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b9bb1aed20c39b65b503d07a98849d82581f7c27))
* detect multi-hop path payment cycles ([941e8bb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/941e8bb242fafc4b4edff89192994cf73d48aa04))
* **detection:** add LSTM temporal anomaly detection for wash trading campaigns ([66b0951](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/66b09518acd23900bd563788d317e133fed71917))
* **detection:** implement SHAP waterfall explanation and cross-chain bridge correlation ([b004c19](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b004c19d8b22eada51856fb951b5162ed926162b))
* expose GET /path-cycles endpoint and add PathCycleDetector test suite ([#121](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/121)) ([6aa53f4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6aa53f4291fdd4fa4673a7432c8065e3e937b9c2))
* feature store cold-tier archival to Parquet with dual-tier retrieval ([d7ee59c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d7ee59c4f146ac3e7d5d8b178c5949755dfcc7ed))
* **federated:** add FL model, DP-SGD privacy utils, and privacy budget API endpoint ([cf88552](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cf885528e284bc0191b2fde4b508b9366dffbca6))
* **federated:** Byzantine-fault-tolerant Krum/Multi-Krum aggregation server ([585e659](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/585e6592732164c24a990278377f25ed19827a2f))
* fix [#31](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/31) cors middleware and real health check ([0da78fb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0da78fbc79a23ee0fa7861ccc4464467872ff6b8))
* full Soroban integration — on-chain score submission with circuit breaker, retry logic, audit log, and --no-submit flag ([a340a8c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a340a8c6fa4a5651c46e34b8e919290ec68289fc))
* implement adaptive Benford window sizing based on trade volume density (issue [#102](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/102)) ([fa063c5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fa063c570b467437b52660530567f22c45b8eded))
* implement API versioning with /v1/ prefix and deprecation headers ([#159](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/159)) ([b87a9c9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b87a9c936b373a6542e7a7273a918ec865579c99))
* implement full migration ([a554f33](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a554f33b3992bb33d64acbb10bc6837eb268a327))
* implement full observability stack ([#46](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/46)) ([9668d31](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9668d3128cf73327fca21811b13c3bb34f08a8c9))
* implement HMAC-SHA256 model artifact signing (closes [#32](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/32)) ([d286b2c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d286b2cc16214b23b15c9a18eac11c8ea4223afb))
* implement Incremental Snapshot Export of Raw Trade Data to Parquet ([ed755cb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ed755cb7f456717e6e8f004bae0e1db7c29f678e))
* implement issues [#164](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/164), [#168](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/168), [#178](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/178), [#180](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/180) ([71e51fd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/71e51fd1ab6a467da8583aa9f6aed01734653196))
* implement Issues [#192](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/192) and [#193](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/193) - TypeScript SDK with Zod validation and HMAC-SHA256 immutable audit log ([6d1d909](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6d1d909ef0bb09ad31398ec261a32154f4a7dbbc))
* implement issues [#89](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/89), [#122](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/122), [#128](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/128), [#129](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/129) ([c38cdfb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c38cdfbef16cf48c1c16a587ddbaa2bddb529bc3))
* implement Multi-Network EVM Provider Failover for Bridge Ingestion ([2e96de7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2e96de7e972ba0db5bde4f76b2dd25fb75be35b5))
* implement mutation testing with mutmut for detection modules ([3f63d57](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3f63d57e5a20123b3f00ac976e2f8a89aa42d9f8))
* implement PC-skeleton causal feature selection (issue [#113](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/113)) ([dfc55bd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dfc55bd5f5856ccb1162ab5848d042d1dcbdd812))
* implement RAPS conformal prediction, performance monitoring, and ensemble stacking ([#109](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/109) [#110](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/110) [#111](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/111)) ([5467dc7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5467dc7fd281a129ef84e79c2c2161d34eb239c2))
* Implement Rate-Limit-Aware Retry Logic with Jitter for Horizon HTTP Client ([e4c37f8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e4c37f84acf8a9e40f155a395d244ccdc0eae377))
* implement red-team campaigns, PSI trend alerting, adaptive reweighter, and SMPC ([7801631](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/78016314f23be33776f470d60b4b9774e23ec377))
* implement semantic versioning, release pipeline, and multi-tenant namespace isolation ([a986280](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a9862807932c4f19ebdac9981b3ce21e55507b5f))
* implement SMOTE variants (ADASYN, Borderline-SMOTE) for class imbalance handling (issue [#105](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/105)) ([963fff9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/963fff9ea6c4dcde7a73be2b8f3baeda2bfe4874))
* implement T-GNN model for wash-ring detection and integrate into training/inference pipelines ([21b6138](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/21b6138118513392a78fe51657829cb667939d5e))
* implement zero-knowledge risk score proofs ([7e63c4f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7e63c4fe885f22bc40160f8af379ba6292b66f33))
* **ingestion:** add bridge event integrity verification ([1892236](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/18922368836543da221886d1f697a82db253a36b))
* **ingestion:** decompose path payments into per-hop Trade records ([7a36bac](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7a36bacb534ec11fc07f46af261b9109c3849ea3))
* iterative Tarjan SCC + CSR adjacency for 100K-node trade graphs ([1fb8be9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1fb8be92c38520b958c04ce561d0933dd75fabb5))
* make ensemble weights configurable ([b4dbbb0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b4dbbb0a4f907924b04599afac959a2e63357b7e))
* online ensemble reweighting via Thompson sampling bandit ([5d10530](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5d105309e9035cad5521c300ae9ca26e41b3f4ef))
* Pedersen ZK commitment scheme ([#147](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/147)) and governance engine ([#150](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/150)) ([1dd95b4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1dd95b4c9703ba5504e319181d0742340b66cee5))
* replace asymptotic Benford chi-square p-values with Monte Carlo bootstrap for N&lt;100 ([2a744c1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2a744c184a5f22c0bdcf1fb5ffce56228d0e42d2))
* replace random train/test splits with temporal splits and purge gap ([03c5deb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/03c5debce2929447d6ecf9899f8ccf3c1a7e7951))
* **retention:** add GET /admin/storage endpoint and nightly archival scheduler ([#180](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/180)) ([bf8d064](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bf8d06487e1db45276a6f9b792a9cf7e219a85bd))
* **soroban:** harden circuit breaker with health endpoint, manual reset, and DLQ ([6f0b628](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6f0b6281333fb5ecbe0f1374e4f9f3e48f02749b))
* **storage:** implement SQLite schema versioning and migration system ([a25d4f0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a25d4f03f490b165ae9b00d8e5559fb095b638ea)), closes [#7](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/7)
* stratify Benford analysis per asset pair for targeted wash-trade detection ([c2192d1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c2192d1f4faf16976120d5980b358f3f814cd610))
* **suppressions:** add alert suppression API endpoints and AlertDeduplicator integration ([#178](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/178)) ([1028813](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1028813ad4f5d3547d96a32a2bc06b31202f936a))
* synthetic adversarial trade generators for model hardening ([a432268](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a432268722a11bc9a78d90e099e05053ed91e19a))
* webhook alert delivery system with HMAC signing and retry guarantees ([a6b75b0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a6b75b06179247822678877ef18a2e8714df0fef))
* wire real-time Horizon SSE streaming into the detection pipeline ([967ec69](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/967ec696577217f88bd6248d773c8ee2140da5f3))
* wire SHAP explainer into API via /scores/{wallet}/explain ([76f8bfb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/76f8bfbaf06084a7e6df168177699de19119c96d)), closes [#4](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/4)


### Bug Fixes

* **#173:** clamp volume_to_unique_counterparty_ratio to 0.0 for negative base_amount ([07112d2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/07112d29853c5d6c1213a7476d0993bf12eda7c7))
* add feature_vectors table as migration v3 (missed after schema versioning merge) ([ca4482c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ca4482c1f37571dedb385fbcc78de1b2160b322c))
* add missing benford_window_expanded_* entries to FEATURE_CONSTRAINTS ([18cc787](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/18cc787166dfc83a52649aee35a1a00bc0359343))
* add missing benford_window_expanded_* entries to FEATURE_CONSTRAINTS ([abd94fa](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/abd94fa6f1ec26845c038c836680e5efd26da5ea))
* add missing benford_window_expanded_* entries to FEATURE_CONSTRAINTS ([42d2f4c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/42d2f4ce615a5ff71296dca1829e47b63d2b7d79))
* add missing stacking functions, _get_health_feature_store, and compliance key fix ([fb51783](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fb517830c89793e7531f2a23e66adc3b144538c2))
* add missing sys import in cli.py introduced by merge from main ([6d614cf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6d614cf8c15ac31fff665297d1be16090960e92c))
* align SHAP persistence with renamed pipeline variables (scored_features/wallets/pairs) ([8d9cc94](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8d9cc94dccb196e4fabd076463690864a0ce46ef))
* bump dowhy to &gt;=0.12 for Python 3.12 compatibility ([b6acc30](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b6acc302f1211495845b38dfabb2467313875da2))
* CI batch 3 — skops types, tracing isolation, analyst feedback URL, mlflow duration, hop_payment_cycles schema ([e3a1717](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e3a1717c52331491f3632bf7c31e017ac1babbba))
* CI batch 4 — compliance root endpoints and meta_learner excluded from ensemble vote ([f7fede7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f7fede7ce58ee8963b65bb1271a75cc884aa92cf))
* CI batch 5 — skip non-model results in metrics aggregation, hop_payment_cycles schema columns, tracing fixture isolation with direct provider assignment and W3C propagator ([fb9c586](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fb9c586d0affd3ef4b276906002f96a4a317aae9))
* E401 ruff — split import tempfile, os onto separate lines ([58d0c60](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/58d0c609d08f38a0a722e146ebf2388501fc1123))
* handle missing settings.model_dir when loading ensemble weights ([0cbbb73](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0cbbb73df5c3c7c7f4ea3121848068444dea7a52))
* implement changes and close issue [#38](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/38) ([b88609a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b88609a0533e5db7995b3ea133c0f034cee16bd1))
* implement Rate-Limit-Aware Retry Logic with Jitter for Horizon HTTP Client ([95be88a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/95be88a5e1ffa8215a95d46290d3123f89b4fda9))
* increase CI timeout and pip install timeout ([e5c2522](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e5c25223c0323c69485a23ac00d1bb67c473ed1a))
* lint errors — auto-fix F401/F541/F811/E402, noqa remaining F841/fakeredis ([e50fb8a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e50fb8acd0542e4365213a0f5cd5fb55a7f1bc7a))
* remove duplicate explain_wallet_score definition (F811) ([3edd45d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3edd45d6bf92eedfa45f88a31dc23750f43cc6c5))
* remove unused Counter import (ruff F401) ([f57c45c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f57c45cabaaed3a2d50003f5982b76a374ce7a1f))
* remove unused dataclasses.field and pytest imports from upstream main ([443717c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/443717c5b76067ffec78ff40b80cba156cd74d20))
* remove unused dataclasses.field and pytest imports from upstream main ([9a77993](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9a77993c6b2ff927659c57f0116cc59a17eb8fb3))
* remove unused imports and variables to pass ruff lint ([b36c20d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b36c20daea32aeb1059813385740ef6b4ec7b733))
* remove unused pytest import; replace deprecated datetime.utcnow ([e8be44c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e8be44c1937cdf36d6f59c9c9eebb60772d77038))
* resolve 118 ruff errors introduced by merge from main ([33f5e31](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/33f5e313214aee88c516fd9d34ca9c0f4de4dfdc))
* resolve 3 remaining CI failures — mlflow artifact path, robustness KeyError ([3adeea3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3adeea38a8f41de205ce7a8c2f928548702d9e87))
* resolve all CI lint and test failures ([bfc7a50](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bfc7a50028addb61d5769eb92bd4fe8df008d103))
* resolve all ruff lint errors (F401/F811/F821/F823/F841) across codebase ([aa16763](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/aa1676327b4d36aa134f6ceb60c297bdaa54ad1a))
* resolve all ruff lint errors (F401/F811/F821/F841) across codebase ([b945dee](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b945dee0ae97aba777464f722deaa3ddf746d123))
* resolve all ruff lint errors (F401/F811/F821/F841) across codebase ([e931b04](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e931b04def850992c2256e30a003bb41dd354bfa))
* resolve all ruff lint failures blocking CI ([66601dd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/66601dd5f72b43eb241b2c416118a71ba62c6307))
* resolve all ruff lint failures from CI ([7fad76c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7fad76cfda2a3cb780f45a8251c567feae8241af))
* resolve all upstream lint errors introduced by merged PRs ([04e2938](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/04e2938af5ae174cf4e4b42bd5658671c8d0274c))
* resolve CI failures — NameErrors, feature dedup, CLI audit command ([44531cc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/44531cc929a658b98c9aca1cbc9ae0c135e34f45))
* resolve missing imports and Settings fields across the codebase ([24a5a32](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/24a5a322a3b5fbab6525376bcb73dfffc1487424))
* resolve post-rebase ruff lint errors (field, pytest unused imports) ([aa27345](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/aa27345b21b92ee837b6498056b0b73e9f28e3e1))
* resolve pre-existing CI blockers for Python 3.12 compatibility ([956450e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/956450e660c4682e33b92044dc20ceda6393d463))
* resolve pre-existing CI blockers to activate GitHub Actions ([52a5748](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/52a57483c4c8f256f939958b14e87b48ae1593c1))
* resolve remaining CI failures across tracing, mlflow, pipeline and analyst dashboard ([e56e296](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e56e296a6fb9a5d9b5c003b75e247bc1de55a6a4))
* resolve remaining CI failures in model training, compliance, and analyst endpoints ([f2d3384](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f2d338452fae544d7b47c6fea398d9c76eeca747))
* resolve ruff F401/F841 errors in graph_engine, test_feature_store_archival, test_iterative_tarjan ([90edae6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/90edae68f448e5e16e0779f911f61729ced62ee6))
* restore causal & multivariate benford features lost in merge ([9b624ff](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9b624ff18b9d5b87936f481155e34b904070213a))
* restore ci.yml indentation broken by merge from main ([ecbb325](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ecbb325e957ab368208c7e759f054ebcaec8387b))
* restore multi_pair param and pair_correlations schema after rebase ([5f5400b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5f5400b9d8b9f79191ad959d7aa56d3c8362300f))
* ruff lint errors — unused imports, unused variables, E402 noqa ([0f533d9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0f533d9f6f4ba975b07f815fee3fa83878cbb5a5))
* ruff lint errors and add stream_with_features to horizon_streamer ([1df8330](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1df83304d02a7e477680a1c8d30130a88af34465))
* skip _-prefixed results when building models dict in test_adversarial ([1e01e0a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1e01e0ac458dab1df08a4fca336157874082840d))


### Documentation

* update CHANGELOG with all unreleased features ([4ced592](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4ced592c15e3508e168c4f0cce69f41a83c7adb1))

## [Unreleased]

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
