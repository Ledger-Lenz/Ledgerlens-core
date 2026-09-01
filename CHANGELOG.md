# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are automated via [release-please](https://github.com/googleapis/release-please-action)
(`.github/workflows/release-please.yml`, `release-please-config.json`,
`.release-please-manifest.json`) — merges to `main` with conventional-commit
messages drive version bumps, this file, and the tagged GHCR image publish.

## [0.2.0](https://github.com/Ledger-Lenz/Ledgerlens-core/compare/ledgerlens-core-v0.1.0...ledgerlens-core-v0.2.0) (2026-09-01)


### Features

* add Atheris fuzz harnesses for all ingestion Pydantic parsers ([#352](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/352)) ([493e360](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/493e3600bacc558f9a4217b680c5713843a7c35d))
* add Atheris fuzz harnesses for all ingestion Pydantic parsers (issue [#352](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/352)) ([38fff85](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/38fff85330e79c76951992269066a09b1fbaad6c))
* add cluster-level chaos engineering suite using Chaos Mesh ([32d3402](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/32d340293a3068f5b7da4f0b92ed7a1073ef0429))
* add cluster-level chaos engineering suite using Chaos Mesh ([edaa953](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/edaa953a96fad359be4ea2e624e4fef2c8cea3bb))
* add GraphQL API layer with wallet query federating scores, SHAP, and cross-chain links - Closes [#337](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/337) ([65a32a2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/65a32a29c0536ae11d0efd0a59c2d4c678eda3a4))
* add homomorphic-encryption aggregation to federated server ([2c6de98](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2c6de9838fdb3f0ebaf6c3837cb76162b6c8bb62))
* add multi-region lease support and configuration ([707257c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/707257c4a61772f6ea3e6981a15577df68bca4ce))
* add multi-region lease support and configuration ([f44b52f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f44b52f81300366fa58110ad89e26dececd09030))
* add multi-region lease support and configuration ([43b1709](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/43b1709aab2b0fa4b1e844b4a723e96164ceaafe))
* add progressive delivery with Argo Rollout canary and CD workflow ([b97c179](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b97c1790f421361a9a94f99252005faff188c79d))
* add progressive delivery with Argo Rollout canary and CD workflow ([9d2ff58](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9d2ff585e8ae27dbfcfd81ce73199b0ba652aa24))
* Add Rust SDK crate (crates/ledgerlens-sdk) with client + ZK verification ([26def77](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/26def77ed51089dfcf40dac4435015ce514f2f1d))
* Add Rust SDK crate (crates/ledgerlens-sdk) with client + ZK verification ([01f60ee](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/01f60ee3c3fe4a1284be0c57bd36da27432645b2))
* add SHAP drift monitor with PSI/KS-test and input_stable_but_shap_drifted detection ([f291dc3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f291dc311e2586892895e27fda4600a5e0fb963c))
* add SHAP drift monitor with PSI/KS-test and input_stable_but_shap_drifted detection - Closes [#345](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/345) ([27dd1e1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/27dd1e1f060e9d954e0a42c3a0982ce06ea6d81f))
* analyst case management with claim/release, soft locking, and SLA tracking ([f5065a0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f5065a0becad9463699ae46fd7dd91983f62b951))
* **chain:** durable retry queue for on-chain dispute overrides ([9824044](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/982404468a3c377eaeb3e1e6e0eb744003e55900))
* **chain:** durable, resumable retry queue for on-chain dispute overrides ([3944c15](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3944c153570c044bee3712b456b7a6086198ea4e))
* consolidate three-way auth duplication into single GatewayMiddl… ([c28fb00](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c28fb008cabb5584b80d224f7c6a4f1eadad700b))
* consolidate three-way auth duplication into single GatewayMiddleware ([5f9549a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5f9549a6718f37a89fcaaa8bc8788d7b68709e90))
* **drift_detectors:** add comprehensive test suite covering _combine_stats, ADWIN, PageHinkley, DriftDetectorRegistry, and module config ([28dcf65](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/28dcf65fc83570becea8d089b97671d87b5c4530))
* **drift:** add streaming ADWIN/Page-Hinkley detectors coupled to conformal recalibration ([f544649](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f5446491d35e0af35e8b506d546a3e873c08b6e3))
* enforce cross-repo schema contracts via shared fixtures ([4f05e28](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4f05e28e172a3f6e96425e71f4506711ecf4fde8))
* enforce cross-repo schema contracts via shared fixtures ([23f0e0c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/23f0e0cea16a994db8097e1c5261c754fdcb0226))
* **feature-engineering:** Numba JIT acceleration for hot loops ([#347](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/347)) ([eab5697](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eab5697181ec4faabc1bd582262c116817e341d8))
* **feature-engineering:** Numba JIT acceleration for round-trip and cross-pair hot loops ([7542fd0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7542fd0e09fca0dc1d5c2d3f1df818a48bc0fe93)), closes [#347](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/347)
* Foundation for PostgreSQL migration (Helm, Config, Migration sc… ([3ca58bf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3ca58bf8ccc3ebceeca4d6398b0f8b32dacdea6f))
* Foundation for PostgreSQL migration (Helm, Config, Migration script, initial ORM) ([2b91943](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2b919436404d541104f5cd397cd0553c7aa38bf4))
* **go-sdk:** add Go client library for exchange backend integration ([#340](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/340)) ([5498386](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/54983869a9bb479339981f324af96823865e3f30))
* **go-sdk:** Go client library for exchange backend integration ([f295104](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f295104c2a31ff98a48a9b42ff0b7141d422e4b1))
* **graph-engine:** add adaptive sharded graph engine for million-node-plus ring detection ([71737d6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/71737d6d6e5aa5e61eafac67a72cfbcfca2d3863)), closes [#348](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/348)
* **graph-engine:** build adaptive sharded graph engine for million-node-plus ring detection ([d478223](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d4782232a92681c13c6fd572f3ca4db67dd76289))
* heterogeneous GNN graph schema for asset-mediated ring detection ([a4f3e65](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a4f3e6590e3c3708abd5f2d651a196407631937b))
* implement gRPC internal scoring service for low-latency score d… ([e632f0e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e632f0e5b6b3f93b1728ae6826e90bfb76c75b1b))
* implement gRPC internal scoring service for low-latency score delivery ([#338](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/338)) ([3df2051](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3df2051b942ab79ea822b0984f2aa3ec6ca6ffd1))
* Implement Kafka/NATS Event Bus for RiskScore handoff ([8ec7b76](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8ec7b7646dd8af0a84a310e00a6c73782475361c))
* Implement Kafka/NATS Event Bus for RiskScore handoff ([c54afc2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c54afc2f327064fc1e3b8cd9cc4df377b75581f6))
* implement overlapping validity secret rotation for keys and web… ([b0ea0d8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b0ea0d8f4ddf936214b79ff951aa31b74f335f8f))
* implement overlapping validity secret rotation for keys and webhook secrets ([6365745](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6365745b9ca1149e465e94ac06294ad34d5feeef))
* implement standalone federated learning server with cryptograph… ([5936912](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/593691201e15c66e7ad08f727509d6cdc453229d))
* implement standalone federated learning server with cryptographic audit logging and Krum aggregation ([5e7d844](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5e7d844c9cc889f6454ff916efd868f534a0c0ff))
* implement unified idempotency and deduplication layer for inges… ([ebbf95e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ebbf95ea08bbe83428637dedc0d3246fb17c715c))
* implement unified idempotency and deduplication layer for ingestion ([16f429b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/16f429bb51eb010785db8b04a13d3d0099ee8f0b))
* **infra:** implement dual-layer chaos engineering strategy ([#694](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/694)) ([1d199bf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1d199bff3b2426ea66904f32ce7dd3567f29b00d))
* **slo:** formalize SLO / error-budget framework with burn-rate aler… ([468ce5b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/468ce5b240fed2ecfcac2b66f9e91ed5ddaf522a))
* **slo:** formalize SLO / error-budget framework with burn-rate alerting ([6485421](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6485421a1d1ec7f4310801af62619bb16a5c97d7))
* zk-snark backend ([cd79d5c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cd79d5c6d511d9f19083bf88ca5dc8f81d5bfaad))
* zk-snark backend ([c2dae81](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c2dae8154cce8a772ec5742909c7ad1b6cccbf56))


### Bug Fixes

* **#499:** cleanup ingestion/rate_limiter.py ([0bd2d31](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0bd2d31fd1f8d24375d1e0492fddf2b7c03827e5))
* **#499:** cleanup ingestion/rate_limiter.py ([9b3887c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9b3887ca9a80e547daf4a2d6db32540f3df03103))
* **#500:** cleanup ingestion/replay_buffer.py ([01bdefc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/01bdefce2403a119284cb0801bd86ef317a874fe))
* **#500:** cleanup ingestion/replay_buffer.py ([477b3c4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/477b3c49a6fbb2ed1a59de1dc591bdb308900851))
* **#501:** cleanup ingestion/solana_adapter.py ([0d2cac8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0d2cac88b7db8c255613263b62b1ea2b4c6e25f4))
* **#501:** cleanup ingestion/solana_adapter.py ([6676929](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/667692950164fa1c5e4c35b1c0c9a62fb9af6f11))
* **#502:** stabilize tests/conftest.py ([227858e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/227858e969e202f00383734dcf5c2f437d3b420e))
* **#502:** stabilize tests/conftest.py fixtures ([d31dc0d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d31dc0d1df8c159a7a53d9280859c3f8612f6a46))
* **#523:** stabilize test_cursor_checkpoint.py — scope os.replace patch to module ([a348025](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a34802546fa6b36db955c052333505686fafedec))
* **#523:** stabilize test_cursor_checkpoint.py — scope os.replace patch to module ([04186c3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/04186c39c9fbdd0c3e56f9999bc8db6661f518e4)), closes [#523](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/523)
* **#524:** stabilize test_ingestion_dedup.py — remove hidden state, stale imports ([9226941](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/92269418f205561158592db2b65227ca05944294))
* **#524:** stabilize test_ingestion_dedup.py — remove hidden state, stale imports ([5d243f1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5d243f10a1ec3ef9e3ed04bb27c02f2e6cd78479)), closes [#524](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/524)
* **#525:** stabilize test_lineage.py — fix thread/fixture ordering, monkeypatch all settings ([df7cfba](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/df7cfba8ed59af78ff4fe59e4a1d911051da5a53))
* **#525:** stabilize test_lineage.py — fix thread/fixture ordering, monkeypatch all settings ([be57b86](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/be57b863616164984df2c945940df505c4c79e32)), closes [#525](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/525)
* **#526:** stabilize test_cost_metrics.py — remove hidden state coupling ([151672f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/151672fc6f5b52830c2fe5c5b4b52d4f2b80c863))
* **#526:** stabilize test_cost_metrics.py — remove hidden state coupling ([e134ac1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e134ac1535e3955528251baeff99314147f75664)), closes [#526](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/526)
* add from __future__ import annotations to storage.py to resolve SandwichCandidate NameError at module load ([bbe12a4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bbe12a453e72542714218ef2cfe10a2ba2a7c5f1))
* **api:** add bounds validation to min_score on GET /v1/scores ([51a3f48](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/51a3f486a64f0ec6b0feffa71d333ccf95da1671))
* **api:** add Query ge=0, le=100 bounds validation to min_score on GET /v1/scores ([#682](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/682)) ([42a70f6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/42a70f63c60881cc807a9b629ffd2d034abef062))
* **api:** validate score filter bounds ([9c5c208](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9c5c208679e0634ae22f14ff142b10f1812f81ac))
* auto-correct overly permissive checkpoint file permissions ([#484](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/484)) ([b0331ab](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b0331ab99874e5feaf8c3611ee735414607190c5))
* **benford:** add missing pvalue keys in n==0 return, modernize type hints ([a61e013](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a61e013f060da118a8204b82ba2c747c3bde3c20))
* cargo-fuzz manifests can't inherit workspace.dependencies from themselves ([f46e9b3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f46e9b3afb469b45ddb5fd024402c5f1fdd6459f))
* **chaos-tests:** stabilize tests/chaos suite — hidden state coupling, dead code, proxy leaks ([#507](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/507)-[#510](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/510)) ([f9a93f6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f9a93f64a8cc606c7f02edffcf8e3f44e86a29f8))
* **chaos-tests:** stabilize tests/chaos suite — remove hidden state coupling and dead code ([#507](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/507)-[#510](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/510)) ([dd2da9d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dd2da9dc9ed0e0a7fd4742565c62de5a3909d877))
* **check_vuln_waivers:** parse govulncheck's real pretty-printed JSON stream ([c390d09](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c390d09d0ff4749c759818e8137353cec1d25984))
* clarify filters thread safety and exception handling ([#490](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/490)) ([06694f6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/06694f607366277c110d25171d48926b17e44f38))
* clean up deprecated patterns and stale imports in api/ws_router.py ([9930043](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/993004348623f6d612e595eb7eb6a9e86a7ca2c9))
* clean up deprecated patterns and stale imports in api/ws_router.py ([f0bfd4a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f0bfd4ab2871255f7d6d679ec5b89eb11d4df18c))
* cleanup config & detection modules — stale imports, missing fields, error handling, and tests ([e8daa57](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e8daa577fe86077eae07f8e4fea04d3f4edf8992))
* cleanup counterfactual_constraints — remove duplicate GNN constraints, lazy validation ([1c42e38](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1c42e38e050dc82c74bb4e1ffd5eba423ff8740a))
* cleanup counterfactual_engine — extract constants, add docs, from __future__ ([12b00a0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/12b00a031913ca1cb2abaf0de547a26faafa6a86))
* cleanup counterfactual_translator — lazy initialization, thread-safety ([ce92ae8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ce92ae8a0d6171f2bde225c5cf07821e155623a2))
* cleanup cross_chain_correlator — add error handling, debug logging, type hints ([4558433](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4558433e848d7bfb3915549b175dd10d81b43eb0))
* cleanup detection modules - ensemble_reweighter, embedding_store, drift_monitor, drift_detectors ([88934c9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/88934c9fe4a29144f6f652b893ddf3af4180a403))
* cleanup detection modules — remove duplicate constraints, lazy init, error handling ([3a947ec](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3a947ec03706e5b54fa661304041a5ab980af3b7))
* cleanup detection modules — remove duplicate constraints, lazy init, error handling ([de2887a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/de2887a31e5221cea4ec132b4780037f5cace331))
* **config:** add missing cost/capacity field declarations to settings ([9920384](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9920384578e8ae81d69da8185e865e18c64fa236))
* **config:** add shutdown_telemetry, improve error handling in telemetry ([789b949](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/789b949bb38fabd8a1c7e3beb4b37c365f0e3403))
* **config:** extract _ServiceFilter, add type hints to logging_config ([f9b2128](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f9b2128af51fbb20ceac8cf343882b637dfd104b))
* **config:** remove duplicate settings fields and add regression test ([7bdd42d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7bdd42de0f86ca51f62ff14054f0f4d42866d918))
* consolidate Decimal import and improve fallback decomposition accuracy ([#498](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/498)) ([210d357](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/210d357ded0d7e600c6becd4e263888ede7ad4f3))
* consolidate imports and remove dead code in api_key_store ([c515a7e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c515a7e305010f5a51aad5f58ae7db22263345fd))
* **contract-client:** stop logging uncertainty bounds as if persisted ([74cd9c1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/74cd9c1ae966ad00a57111fbba67b942807a0a68)), closes [#700](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/700)
* **contracts:** make both Soroban crates build and test again ([c1ad377](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c1ad377f47b4c102df0eb5a2bb6fd1f295352b36))
* deduplicate _first_digit and add missing tests for benford_baseline ([4fd8aff](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4fd8aff46eeadea91d96e7db8757b648892d2a11))
* **detection:** clean up scoring explainability utilities ([ec89dee](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ec89dee000c2e7c7e70bfb118126ab130d509de2))
* **detection:** clean up scoring explainability utilities ([0c75a74](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0c75a74642a5e42c6a06156878d0c179edf051c4)), closes [#467](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/467) [#468](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/468) [#469](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/469) [#470](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/470)
* **detection:** modernize type hints, improve SQL safety, add test suite for alert_engine ([def8944](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/def89446360323bd9f6a93193531e683aeef0426))
* distributed per-API-key rate limiting shared by REST and gRPC ([2e1f791](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2e1f79106719c4b3f64765a439bb3c09f037c4a5))
* distributed per-API-key rate limiting shared by REST and gRPC ([3b771bc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3b771bce26b3a3fac7ab79e30feb11c03009b8d0))
* **docs:** improve report a11y and document zk circuit and chaos helm values ([6625306](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6625306cff7f7c0effc764a8dd211c2195c7cacd)), closes [#807](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/807) [#805](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/805) [#803](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/803) [#802](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/802)
* **drift_monitor:** resolve duplicate DDL collision — rename DriftMonitor table to feature_psi_trend, add alert_type to degradation_alerts DDL, remove ALTER TABLE workaround ([062c9d3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/062c9d3cdf1fb639cc6e9343359bfd99144d1698))
* **e2e:** resolve cross-repo checkout ref-pinning and stub contract deployment for [#695](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/695) ([30cf35c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/30cf35ce349f089331cb2dd313012894c10ccbe4))
* **e2e:** stabilize e2e tests, remove hidden coupling ([#503](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/503)-[#506](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/506)) ([6a7d632](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6a7d632dc4dab7a71e70dcce940c204731ec276e))
* **e2e:** stabilize e2e tests, remove hidden fixture/global-state coupling ([#503](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/503)-[#506](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/506)) ([eed77d8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eed77d8e48d6091fe238fe6cd387f9c0c41a3907))
* **embedding_store:** add optional computed_at param to upsert_embedding, modernize type hints to union syntax ([77cccba](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/77cccbaa2d5c0643afcbb2a337c2e44d498912f4))
* enable testutils feature and fix asset_pair type in fuzz harnesses ([8b98f63](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8b98f63416ca9d70e81c732ced317b9d1ea40195))
* **ensemble_reweighter:** move time import to module level, replace bare except with logged debug, add test coverage for get_current_weights and apply_weights ([287ff91](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/287ff9137e9a30d0a581615011a1dc00535d76d7))
* improve graph_builder type hints and safety ([#491](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/491)) ([b41317b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b41317bea84d0fa8cea230fe4557f943a60742b1))
* improve historical_loader safety and imports ([#492](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/492)) ([a288d3f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a288d3fe32e2e87bd975571dab904e682761296c))
* issues ([8dc7111](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8dc71111fac4931a9ab708b3c3a31954144595c3))
* issues ([ff7ea3c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ff7ea3cd6605adb9060583f7d1a592b53ae57dc2))
* issues 414, 415, 416, 417 ([ac47dbc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ac47dbcae818591c97b6642a3f716d787063dfaa))
* issues 414, 415, 416, 417 ([f3f78d8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f3f78d8846a74bdefea4bde2a082d46e99d9829e))
* issues resolved 410, 411, 412, 413 ([1f424b5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1f424b5f6d85921063bba5702397fb0f0ed73b3c))
* issues resolved 410, 411, 412, 413 ([b5269d3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b5269d36c0c9489e9337c52eee2cadbe86e792f4))
* **mlflow_tracker:** upgrade log_hyperparameters and log_metrics failures from debug to warning ([7a4f4dd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7a4f4dd716d6e1c00fab28226b425ee70d5bdfda))
* **model_card:** replace naive markdown-to-HTML in render_pdf; populate fairness_summary ([9ac426d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9ac426daf4da7d148df55126d8a24abf7ebc51d3))
* **model_registry:** add error handling for missing dirs, corrupt JSON, and SHAP persistence ([e77d5e1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e77d5e1b1a6c56134aeaf4de212e0e741b65282d))
* move requests import to top level and simplify wallet filtering ([#485](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/485)) ([d389fed](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d389fed18435ded509f4caa1e44bfe90aa6541e3))
* normalize config behavior and remove drift in correlation.py and cost_exporter ([6e49897](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6e49897ae56cee30e62ac458db7e548ae9f3edad))
* normalize config behavior and remove drift in correlation.py and cost_exporter ([89bb1c2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/89bb1c241748b70acffc22fa343ef3a053143b33))
* **oracle_aggregator:** make contract compile, fix quorum bypass and wire format ([3022933](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3022933479cbf8bb998110928b7f59a8b88b03c0))
* **oracle_aggregator:** make contract compile, fix quorum bypass and wire format ([96f2538](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/96f2538893c86f19a2c2743c600cb3c1037f6570))
* **oracle_aggregator:** unblock the fuzz CI job so zk_verifier's targets actually run ([fd64c34](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fd64c347ad8d9c53b92c092bb9e15e93940f6a8c))
* **oracle:** require auth on OracleAggregator::initialize (issue [#688](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/688)) ([05cd9cd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/05cd9cd1801c95b90d74df7e04624f35603ede7f))
* **oracle:** require auth on OracleAggregator::initialize (issue [#688](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/688)) ([3c3f49e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3c3f49e1f8124dd3bb3620c00cb648e986b38fda))
* pin ed25519-dalek/rand/rand_core in contract manifests for fuzz build ([dcf69dd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dcf69dd4b9d5b5fc882b67867c2adbe2c04da566))
* refactor and cleanup detection module files ([#471](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/471)-474) ([8952ebb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8952ebb26b8f06c2eb0c927b8e6c37af303bb575))
* remove code duplication in operations_loader.py ([#496](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/496)) ([fe70cc1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fe70cc1a6524b8dea04fc3d123d8b198fcb04849))
* remove code duplication in operations_loader.py ([#496](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/496)) ([468ca6b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/468ca6b9eb5500e09c72faf9e2c5b9cb1905fb10))
* remove dead code and clarify BridgeTransfer initialization ([#486](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/486)) ([ede4f7f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ede4f7fdae121fb79c8f50d179d7ee5660103199))
* remove dead code and clarify verification field usage in bridge loader ([#483](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/483)) ([2db1af5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2db1af58039663763ab2b6a0ba70723caf9bbc21))
* remove unused imports, extract magic numbers, improve exception handling, add type hints in storage.py ([#472](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/472)) ([d28dc43](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d28dc438c8fa928ccf218424dcb1e035a0ae451c))
* remove unused VerdictType alias in analyst_store ([e7317f8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e7317f8d6798418e3d785319c55d5b6426b0a743))
* remove volatile singleton pattern and standardize type annotations in suppressions.py ([#474](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/474)) ([4b165db](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4b165db71f1355c76951365e64039b64a668267d))
* replace assert with proper error handling ([#489](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/489)) ([88d8b01](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/88d8b01b44947e1b18b0c8e389697c9bb43b9b59))
* **report:** accessibility improvements to compliance report template ([49ff25a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/49ff25af6cdca837975dce8caf63dcab97090f2a))
* **report:** accessibility improvements to compliance report template ([699e39c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/699e39cd1f15e1c090b175eb05e1ffebbb034757)), closes [#806](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/806) [#808](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/808) [#809](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/809)
* Resolve compilation errors in zk.rs for ark-ff 0.4 compatibility ([f641da1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f641da17e14ab4d0eab0e7406ef93ce79f8ff3df))
* resolve exception handling and config defaults in soroban_lease.py ([#471](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/471)) ([1784613](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/178461360411502efba336898a90399f451c989e))
* resolve repo-wide ruff lint errors, regenerate OpenAPI schema, fix fuzz Cargo.toml ([b99100d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b99100d70a44530f9396319131c41b694047f13a))
* resolve zk_verifier build errors by adding Fq::is_valid and converting from_bytes calls ([5e63d60](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5e63d605257173b9d95923f36b26e6d79ed83fae))
* stabilization cleanup for detection modules ([559858f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/559858f2c4467c04b047c7c0401696ee1a60fc1b))
* stabilization cleanup for detection modules ([f8972b3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f8972b387146dcd41dab7b984aeafa69425a4d3b))
* stabilize amm_engine and fix storage.py SandwichCandidate import ([65d0e3f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/65d0e3fff38fc1b70b0c342b21f2f8df8f025fe0))
* stabilize horizon_streamer dedup logic and error handling ([#493](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/493)) ([5aaf2fd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5aaf2fddb538ddff55d6ba9c91a8fcf4eb44b15e))
* stabilize test_model_registry.py, remove hidden fixture/global-state coupling ([eeee590](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eeee590eeee9819a9b61c125903886fd1522c200)), closes [#590](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/590)
* standardize type annotations and extract magic numbers in streaming_features.py ([#473](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/473)) ([38ae05b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/38ae05b03ec96c82f136bc9f9b55d00d854ebee7))
* standardize type hints to union syntax ([#488](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/488)) ([25f5471](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/25f54715601b932d6a801127989d3f5887cc3f1f))
* store admin identity for ZkVerifier submit_score ([a8c9182](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a8c91826780921c056d858f0926fb6979832e80a))
* **test_model_registry:** remove hidden fixture/global-state coupling via object.__setattr__ ([05a10f6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/05a10f68ceb03ca70d731fe64afcca438901c479))
* **test_model_registry:** remove hidden fixture/global-state coupling via object.__setattr__ ([d451c76](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d451c76931d7780366b8b0d5df7bf001c4441134)), closes [#590](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/590)
* **test_model_registry:** remove hidden global-state coupling and stabilize tests ([cad9beb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cad9bebe6574b421b439add7e2b5482e1c043997))
* **tests:** stabilize test cleanup — remove hidden state coupling ([#519](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/519)-522) ([814f24f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/814f24f3f933e5fd92b20b4dea4e486778fad088))
* **tests:** stabilize test cleanup for issues [#519](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/519)-522 ([d10324a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d10324a2f8769c602380632db98ac315a2bacb01))
* **tests:** stabilize test helpers and remove hidden state coupling ([#527](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/527)-[#530](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/530)) ([2c550c2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2c550c28b03390688a82045e47454b5ae5d0927a))
* **tests:** stabilize test helpers and remove hidden state coupling ([#527](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/527)-[#530](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/530)) ([82cef2b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/82cef2b022110044e96a74a64a91b9907bfa17b0))
* **tests:** stabilize test_api_gateway.py — fix settings isolation and DB coupling ([5b67de2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5b67de23e112bfc8508b06248c3dead33883a442))
* **tests:** stabilize test_api_gateway.py and remove hidden state coupling ([0ccd9b0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0ccd9b056afc5c78e6fe50f9c7d908d477330d5d)), closes [#513](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/513)
* **tests:** stabilize test_api.py — remove cross-file coupling and fix path/fixture bugs ([ee1a931](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ee1a931d7f0f5172592f4bcbb81a68e97c1caa6f))
* **tests:** stabilize test_api.py and remove hidden state coupling ([1e083f9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1e083f97c3ee0e0cadfbad5574d89622e642b1c7)), closes [#512](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/512)
* **tests:** stabilize test_feature_store.py — remove time.sleep and eliminate timestamp coupling ([4c7ddd6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4c7ddd60c4c1c69c244faea0e093aa2690ac15d2))
* **tests:** stabilize test_feature_store.py — remove time.sleep and fix hidden timestamp coupling ([c4d59e7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c4d59e7169bb72ee9bbca650ded13487bd752254)), closes [#518](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/518)
* **tests:** stabilize test_health_check.py — remove dead API refs and add DB isolation ([aab7bfa](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/aab7bfa6b165c37568eb40cee8c1e851d318ff76))
* **tests:** stabilize test_health_check.py and remove hidden state coupling ([bcd41b7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bcd41b7b2d5211a1de22261c42c964d5f999fba6)), closes [#514](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/514)
* **tests:** stabilize test_settings.py — add isolation fixture and regression coverage ([dc37d42](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dc37d425266a2f6441e8b88a19003d215862cb77)), closes [#515](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/515)
* **tests:** stabilize test_settings.py — isolation fixture and regression coverage ([7ca315f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7ca315ffccbe481e287ab053a9276996f9691b1b))
* **tests:** stabilize test_sqlite_wal_lock.py — fix tuple fixture and wrong API path ([f30702a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f30702ab94db238ef8cd5c778024e5dba8f49a22))
* **tests:** stabilize test_sqlite_wal_lock.py and remove hidden state coupling ([82e17e4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/82e17e4523b5b2ad92f0b249e341ef9d773ef41c)), closes [#511](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/511)
* **tests:** stabilize test_storage.py — extract shared fakes and remove duplicate dead code ([2a2f3ad](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2a2f3ada00aa819fad2f49c1b2ef2e8241cc9e0b))
* **tests:** stabilize test_storage.py — remove duplicate fake classes and dead imports ([6aa7dae](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6aa7daef4339debed0fe688e6e990c59bd4cb4ce)), closes [#516](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/516)
* **tests:** stabilize test_streaming.py — fix AsyncMock pipeline protocol error and remove stale TYPE_CHECKING import ([21681df](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/21681df8f23bc1d189582a37556712cb19119732))
* **tests:** stabilize test_streaming.py — fix failing AsyncMock pipeline and remove stale TYPE_CHECKING import ([a635815](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a6358157c8938c7444b298112f4b8ef6e444fd78)), closes [#517](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/517)
* two ruff lint errors blocking CI, unrelated to zk_verifier ([2aac26b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2aac26b6ef7660396e54b0d5901a5a6864b7993c))
* wire Krum/Multi-Krum into production aggregation and fix its broken default constructor ([f3d728a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f3d728a32766064f208aa3b66b4623486c6737d6))
* wire quorum scores to registry ([#684](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/684)) ([6030f39](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6030f39306cfce32693b14970d9d04f768ba7563))
* **zk_verifier:** correct Fp12 inversion (pairing) for BN254 pairing ([0e90ec3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0e90ec3b83c33c799281b270501a1254ed3f5884))
* **zk_verifier:** correct Fp12 inversion so BN254 pairing works ([f053da1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f053da14065c8a9c13643393f5e90dbbbeb6b944))
* **zk_verifier:** make contract compile and actually verify proofs ([390ff46](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/390ff46fcebaf2379dc3c907b9df987faf4cd51f))
* **zk_verifier:** make contract compile and actually verify proofs ([bbc4958](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bbc4958a44e622837e70a10388606ce6b52e42c8))


### Miscellaneous

* add structured GitHub issue templates (bug report + feature request) ([7fc98b2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7fc98b22888965ec6b1d70e936749ad51e9cc6bb))
* add structured issue templates for bug reports and feature requests ([#714](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/714), [#715](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/715)) ([fb74d81](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fb74d817257a56a955903d7d8540b6859fbfdc5d))
* cleanup detection modules ([0aedc8f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0aedc8f7cb10856b5f1b88f247839b0add836d88))
* cleanup detection modules - stale imports, Optional to union syntax, lock-in test ([ea36816](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ea368164430ec7ee8d7246445209014568df1cc1))
* cleanup detection modules - stale imports, Optional to union syntax, lock-in tests ([07fea91](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/07fea91a92e4620ec830d901cd4ded9ca1c2b7f6))
* cleanup detection modules (model_registry, model_card, mlflow_tracker, lineage) ([d1685e9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d1685e9543d582d5b93b28359f90e4a049b2712f))
* **model_signing:** replace typing.Optional with X | None union syntax, fix test allowlist for ed25519, add lock-in tests ([88c9683](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/88c96831846e97a4065c0ee7647c82a815b56fdf))
* **path_cycle_detector:** move heapq/AlertType imports to module level, add lock-in tests ([fdaba7d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fdaba7d02d90ed4abbdda656af1633a4768bbde9))
* **rate_limiter:** replace typing.Optional with X | None union syntax, add lock-in tests ([795ac19](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/795ac19e2767d8daf69035153966de0e92f9f67f))
* **risk_score:** add future annotations, trim docstring, add edge-case and lock-in tests ([3a14596](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3a145963f33becd03841e31edbf523744b6306ff))
* **sdk:** add ESLint and Prettier configuration ([381a4e2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/381a4e2024d1d46395a4f6a0d480c77feb1a3f2a))
* small cleanups across CLI, docker-compose, and chaos-mesh script ([24aa4fb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/24aa4fb77e73e3a8ba23e11f8d0249bd209267b7)), closes [#823](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/823) [#758](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/758) [#756](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/756) [#755](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/755)
* update OpenAPI schema - regenerate ([976c73d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/976c73dff3419246dd2e51d3ed29870aeeb2e926))


### Documentation

* add backtesting/ and helm/ READMEs plus make help/clean targets ([5db97e0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/5db97e09dd7a45dc58d50f7c0ca36a43b8d3d665))
* add backtesting/ and helm/ READMEs plus make help/clean targets ([b254284](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b2542841629bd58b3d11ddbd9c74e5e95f909e7b))
* add Cryptography, Observability, ML & Detection, and API nav sections ([#723](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/723)-[#726](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/726)) ([138a09a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/138a09a0b929aa991ced23a44136a0faeb07ca6e))
* add Cryptography, Observability, ML & Detection, API nav sections ([#723](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/723)-[#726](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/726)) ([32b794b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/32b794b2b16b419cf8a865a69d095ac7b1322251))
* add detection/README.md ([debf8aa](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/debf8aaf901057ce46adeb835dd707e47b8981fa))
* add docs/README.md index explaining the documentation set ([7d4baaf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7d4baaf9ef715ba5a4631672d0764ee2e329ac44))
* add docs/README.md index explaining the documentation set ([2587ee8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2587ee898cea7acf1c68b200ee68f8f56aba5f5a))
* add engineering roadmap index (issue 625) ([c0b6763](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c0b67639d48357572f06459c563e5160cb06f290))
* add executable architecture guide (issue 624) ([04adcc0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/04adcc04da4d0e1ff8f14e4c8f893b244ec1617f))
* add ingestion/README.md ([78ba736](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/78ba7360d288c61dce6e4ad4af143cac037d5b3d))
* add ingestion/README.md ([1b77a95](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1b77a9537ff311963534ecab0fc1f480bbcd9826))
* add ingestion/README.md ([d24d5f2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d24d5f2647b7be87c4b05f132f39377b8868ab8b))
* add inline comments to setup_trusted_ceremony.sh ([17817b6](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/17817b6b648314e5bc87968e59cc74686de36292))
* add orphaned nav sections to mkdocs.yml and api/README.md ([50d4c7a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/50d4c7a8f4514a516065783284b591105a0e650d))
* add orphaned nav sections to mkdocs.yml and api/README.md ([368d4cb](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/368d4cba9628e9f7c4dec3615a61aa70d87197ed))
* add README to monitoring/grafana/provisioning explaining dashboard auto-provisioning ([2560ab0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2560ab06a127ab40aaf9750551dcad566905e321))
* add README to reports/ directory documenting CI-generated license and vulnerability reports ([1068b4f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1068b4f15119757307e9134ed58146be1f711d11))
* add README.md to sdk/ and utils/ directories ([25354bf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/25354bf8368478af88e2166e486480fe92df84e7))
* add README.md to sdk/ and utils/ directories ([a43a1aa](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a43a1aacd29663b470b6a49c86acd21fff9c9cf0))
* add READMEs to storage/, config/, audit/ and move MIGRATION_NOTES to docs/ ([f6f2ebc](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f6f2ebc991c26cefd924d432d21f10f3520ca3c7))
* add repo layout, dependency guide, DoD checklist, and glossary ([4257e18](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4257e18afcd60e2b632655c3d0ef10edb075a2af)), closes [#838](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/838) [#837](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/837) [#833](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/833) [#834](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/834)
* add Security & Compliance section to mkdocs nav ([1bb5bc2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1bb5bc2319d3ccd671d267c413a1e40af2c8f460)), closes [#722](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/722)
* add SECURITY.md, .editorconfig, and contributor/help docs ([ca0a0ab](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ca0a0ab7540e7f2d2e09b83716207aad6df8372d))
* add troubleshooting guide and pipeline type hints ([ee87a76](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ee87a763825a70ce1fe3da167b8e11ca7587aff3))
* clarify Alembic workflow and zk test fixtures ([d753bc4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d753bc4c61fc9f7710c4a24bbc22163f5cfd01e2))
* clarify migrations and zk test fixtures ([4849396](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/484939625a6d9272d85a900c537f497f9789561f))
* **cli:** sync cli_reference.md, document proto regen, improve cli.py errors/help ([0bb8875](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0bb88750584fe93da06615691adf071a3979b861))
* **compliance:** clarify _benford_chi uses asymptotic p-value for regulatory reporting ([eb974f8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/eb974f8658ec804724a439c9bf575ff46d7be3ea))
* cross-link ZK score-threshold proof system components ([8d4d972](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8d4d972b58599d346981434548c8014ace11b05e))
* cross-link ZK score-threshold proof system components ([a876cd9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a876cd9c23533d7d6068d6f3a2f72c2e8e80ec4d))
* **data:** document known_cases.csv schema and provenance ([41d507a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/41d507a2e56acb8e06adfe9dbda74316ef3090b1))
* **data:** document known_cases.csv schema and provenance ([241df09](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/241df09e849b50161007133ea682ecf65a1f0d04))
* define SDK client binding strategy ([c793c56](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c793c569da9050437b0049eacebb9f1124fe5c46))
* document Docker build steps, panic messages, and add CHANGELOGs ([#791](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/791), [#792](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/792), [#793](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/793), [#794](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/794)) ([3325df4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3325df4c536bb183e36b8881d522b73277915d07))
* document Docker build steps, panic messages, and add CHANGELOGs… ([c229d88](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c229d882a2848eef62b5201b43170491a5f0179e))
* document the requirements/*.in -&gt; *.txt pip-compile workflow ([b5c7e2a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/b5c7e2afef985704eb8527bc591491708849fd75)), closes [#721](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/721)
* document zk-circuit constants and helm chart configuration ([528543c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/528543c1363a46318c458da85002f8c0bd61df4f)), closes [#804](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/804) [#800](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/800) [#801](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/801) [#799](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/799)
* **fl-client:** add CHANGELOG.md and link from README ([73b51a9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/73b51a9007de6f2e6b9f0d84a727fc6ea997cda1))
* **fl-client:** add CHANGELOG.md to packages/ledgerlens-fl-client ([0bee9e4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0bee9e4304f8bccdcb5699ef55d5972fa172a8f9))
* **go:** document minimum supported Go version ([056f02e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/056f02eeb8387694681119898d23c28bc3d0108c))
* **go:** document minimum supported Go version ([8434e55](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8434e5503aa54a22766237ee24b72b453246491f))
* implement STRIDE threat model and automated validation test ([a3365b0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a3365b08bd37bcef0394ccdf8aa6d6a7feb9ca5e))
* implement STRIDE threat model and automated validation test ([1a84859](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1a84859f65b74667ee4ad3a8be9d98aadb9dbdad))
* improve pipeline docstrings, retention logging, and error messages ([350d2f7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/350d2f79b8d65f80ba3edfda6357daf7834abd6d))
* improve pipeline docstrings, retention logging, and error messages ([af3248f](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/af3248f64ea51c41ac0f830f5a7d54f382d78d45)), closes [#821](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/821) [#814](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/814) [#816](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/816) [#817](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/817)
* improve README ToC, CHANGELOG consistency, proto conventions, fuzz mapping ([aa58f4e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/aa58f4e1a8ee7f444f428802c781d5bb1da827ce)), closes [#829](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/829) [#827](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/827) [#826](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/826) [#825](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/825)
* move COST_CAPACITY_IMPLEMENTATION.md into docs/ ([00c0f4b](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/00c0f4b4429ed4f541be98c867e545020c8081be))
* move COST_CAPACITY_IMPLEMENTATION.md into docs/ ([c11ed3a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c11ed3a8a152494cbf55c6d3c98d1f8946a624c7)), closes [#719](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/719)
* move SOLANA_ADAPTER_CLEANUP.md into docs/ ([e3f46e7](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e3f46e79b6c5bc1958e9b7d9db20d7ac84843c75))
* move SOLANA_ADAPTER_CLEANUP.md into docs/ ([4e0e86c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4e0e86c8272dc102fb94b06881fe339ddd26d163)), closes [#718](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/718)
* **proto:** add README describing proto/ layout and codegen relationship ([e6209f9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e6209f9a3a65311b81adac2c79075f9521a96189))
* **proto:** add README for proto/ directory ([f49c1c8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f49c1c865ecfcab9f7419981e380a94e838ba5a7))
* **readme:** update stale 2024 drift-report example dates ([3db9851](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3db9851eb42df297c8108313072cb3dab853bdda))
* **readme:** update stale 2024 drift-report example dates to 2026 ([9150aed](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9150aedc26529dad385dbc147120cf4cc60578aa))
* **rollback:** correct verification-status claims to match actual rehearsals ([9111057](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/9111057fd30001a8a04e49747011bdd0a0629a56))
* **rust-sdk:** document MSRV in crates/ledgerlens-sdk/README.md ([#787](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/787)) ([1d2f2df](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1d2f2dfe61e60132f443fb68d4510cb960c0a57e))
* **rust-sdk:** document MSRV in crates/ledgerlens-sdk/README.md ([#787](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/787)) ([e50deff](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e50deffb196571e2cf5402ea8ba7121a2a4c3b57))
* **sdk:** add CHANGELOG.md and link from README ([#788](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/788)) ([bc172a8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/bc172a8e8c5bf4c3a69546fdc2ab350ce328db5a))
* **sdk:** add CHANGELOG.md and link from README and Cargo.toml ([d152136](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d152136841954a2e3df2fe77876e265bd7bb01a5))
* **sdk:** add CHANGELOG.md and link from README and Cargo.toml ([7f9b7d0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7f9b7d01768d73c2d209e59a7871991f4fd8e5d8))
* **sdk:** add CHANGELOG.md to packages/ledgerlens-sdk ([#788](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/788)) ([454a9f1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/454a9f1227cc7e126e83ca178f20cb7f39d436e9))
* **sdk:** add Quick Start section to packages/ledgerlens-sdk/README.md ([e66d250](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e66d2504f73932a3700e4b46ac7243e8fc1e2958))
* **sdk:** add Quick Start section to packages/ledgerlens-sdk/README.md ([0eaba6a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0eaba6a89a5fc9fa395c3ced52b2d4a5c9c26c35))
* **sdk:** add rustdoc examples to public API items ([fce0b19](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fce0b1984b272f79c7bd9f2ebb99ea1915250f3e))


### Code Refactoring

* clean up account_loader.py and consolidate parsing logic ([#480](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/480)) ([046e670](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/046e67059eaee1c9752e26d5bbfc6d14c7c322b3))
* cleanup detection modules ([0c91f34](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/0c91f343fe50ddee9d4503a2e5302fd09215d929))
* cleanup detection modules- [#1](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1) ([ddb2673](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ddb26733057a76f9f9571aac063b86e913682c60))
* **compliance:** remove unused pandas import, modernize type hints ([1ce3a34](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1ce3a342ae54e3d63a94a7969635ec6cdd9de27c))
* **conformal:** remove dead CLASS_BOUNDARIES, modernize type hints ([4fe3b21](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4fe3b216ddd6df5f3f90d07b2cc54e7d4abbed77))
* consolidate error handling and improve logging in webhook_worker ([#479](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/479)) ([c0069f2](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c0069f210deca229aad428006f32ff1aa9dc5c06))
* consolidate init_db calls in webhook_queue.py ([#477](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/477)) ([3aa04b8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3aa04b845b40539f0455991dfd6fb86bf8b6c09b))
* consolidate init_db, fix masking logic in webhook_registry.py ([#478](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/478)) ([652d5cd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/652d5cd819351ae7cca8ef229d580ce89ffd8b0d))
* consolidate init, add logging, fix type hints in wallet_override_store.py ([#476](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/476)) ([ebfb703](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/ebfb703533c884f9434decaa6e11610e4687784f))
* **cross_chain_linker:** fix deprecated utcnow, improve error logging ([cd08b82](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cd08b8204fe2487e73f37f8a042dcce6be33b136))
* **dataset:** remove unused min_train_days parameter from walk_forward_cv ([81bdf06](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/81bdf06f4b9487611aee07ed4ef49cb361919c03))
* deduplicate span attribute logic in tracing.py ([#475](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/475)) ([2d1506a](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2d1506a0e09453c540d52e28468b66022d55e72e))
* **dispute_store:** fix private IP validation, remove dead code, deduplicate DB logic ([f7014ac](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f7014acb14c0a08087fd38d8f81bc27ba1ff24bb))
* **event_bus:** add thread-safe singleton, replace __import__ workaround, use importlib.util for nats probe ([a602edf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a602edff119b9e53559ab5d8feae2c8e0f6f8716))
* **exceptions:** add module docstring, add dedicated test suite for SubmissionLeaseError ([7e78711](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7e78711cd076170c442586c54e8e33fe6465c596))
* **feature_store:** move import math to module level, fix datetime.utcnow() deprecation, remove unused CircuitState import ([f63193e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f63193e0088ebb5ae299236dd340eda54023dd8f))
* **feedback_store:** remove dead _MODEL_NAMES, narrow exception in _check_feature_vector to include sqlite3.Error ([3823379](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3823379879fe261ef6ba66c0c2dc657d0ca32c76))
* improve logging and consolidate trade filtering in amm_loader.py ([#482](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/482)) ([2bd3037](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2bd30372970918687968ac86037790809adb9977))
* make compute_key static to avoid unnecessary db connection ([#487](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/487)) ([6cc1bca](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6cc1bca07697e4460f58e3713e9d7fd75631bdfe))
* reorganize imports in adversarial_data.py ([#481](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/481)) ([f7533b5](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f7533b5e72356d80a3904c94edc282b45c109fb6))
* **sdk:** dedupe reqwest client builder; docs(go): document minimum Go version ([6545377](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/65453774dfbaadb870e79737deaa2ffd9c43cfa6)), closes [#784](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/784) [#783](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/783)
* **sdk:** dedupe reqwest Client construction in LedgerLensClient ([e888605](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e88860597fd131a28c31a0f708ed07a4fdff8213))
* **sdk:** dedupe reqwest Client construction in LedgerLensClient ([e650097](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e6500976cb0b9b4c03b9de486732101834f351d7))
* stabilize detection modules (feedback_store, feature_store, exceptions, event_bus) ([6e94c16](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6e94c165d9561b1f3d5866cef457e4cf65eb7921))


### Performance Improvements

* **lineage:** use deque.popleft() for O(1) BFS traversal in get_lineage_graph ([6f81d2d](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6f81d2d3742f1cefc70f6853787acf37396d79e5))


### Tests

* **red-team:** remove hidden global-state coupling in test_red_team_attacker.py ([3749753](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/37497530d7fabfcedfeeee8c50473a3368db4251)), closes [#598](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/598)
* **red-team:** remove hidden global-state coupling in tests/test_red_team_attacker.py ([dd373f0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/dd373f095ac263d9694f5209335d6aea0b498735))
* remove unused import in test_feature_engineering_jit.py ([c0637ff](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c0637ff14f75e3d536cb8358bdb7f9012cfbc087))
* **storage:** add unit tests for retention count invariants ([71faf91](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/71faf913d72f4def98c20626eb8d61caf8220513)), closes [#810](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/810) [#815](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/815) [#813](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/813) [#812](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/812)


### CI

* build and test both Soroban contract crates on every push and PR ([a481cbf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a481cbf5afc3a187dfc85ae70288acec25716f6a))
* build and test both Soroban contract crates on push and PR ([a9220ee](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a9220ee866a2f7ee91b5f1abf40391d4c83b351f)), closes [#686](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/686)
* gate CD on CI success and add Trivy scan ([2b2fce9](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/2b2fce92e8f3e2234f5a837a1bbd28b8d33d97e2))

## [Unreleased]
### Fixed
- Added `ge=0, le=100` Query bounds validation to `min_score` parameter on `GET /v1/scores` (#682).

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

### Removed
- `api/adaptive_rate_limiter.py`: unreachable from any real request (its only caller,
  `api/auth.py`'s `require_api_key_scope`, was itself dead code never imported by any
  router) and independently broken (referenced three undefined functions). Rewiring it
  would have required a second, parallel distributed abuse-signal counter; its purpose is
  largely subsumed by the primary limiter now correctly enforcing configured limits
  across every replica and protocol. See `docs/waf_and_rate_limiting.md`.

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
- `detection/shap_explainer.py` updated for the current SHAP `TreeExplainer`
  output shape.

## [0.1.0]

### Added
- Initial scaffold: Horizon ingestion, Benford's Law engine, ML feature
  engineering, ensemble model training/inference, `RiskScore` schema.
