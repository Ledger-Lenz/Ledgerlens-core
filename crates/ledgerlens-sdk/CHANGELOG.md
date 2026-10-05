# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0](https://github.com/Ledger-Lenz/Ledgerlens-core/compare/rust-sdk-v0.1.0...rust-sdk-v0.2.0) (2026-10-05)


### Features

* Add Rust SDK crate (crates/ledgerlens-sdk) with client + ZK verification ([26def77](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/26def77ed51089dfcf40dac4435015ce514f2f1d))
* Add Rust SDK crate (crates/ledgerlens-sdk) with client + ZK verification ([01f60ee](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/01f60ee3c3fe4a1284be0c57bd36da27432645b2))
* enforce cross-repo schema contracts via shared fixtures ([4f05e28](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/4f05e28e172a3f6e96425e71f4506711ecf4fde8))
* enforce cross-repo schema contracts via shared fixtures ([23f0e0c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/23f0e0cea16a994db8097e1c5261c754fdcb0226))
* graph retention, parquet schema evolution, detection benchmark, no_std SDK ([#1058](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1058)) ([e075947](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e07594740f94b083938f63e50ad053266a9f3464))
* SDK conformance suite, TS WebSocket reconnect, Go webhook verify, proto breaking-change CI ([#1052](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1052)) ([e5ac40c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e5ac40c3bbbda5a2a6b32d849ea967ac1deca139)), closes [#987](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/987) [#988](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/988) [#989](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/989) [#990](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/990)
* SDK resilience, generated pagination types, semver contract, shard rebalancing ([#1071](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1071)) ([56ade25](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/56ade25d9e5c4660de5010b3ad1fe5c12536f714)), closes [#983](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/983) [#984](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/984) [#985](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/985) [#986](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/986)


### Bug Fixes

* repair CI-breaking compile errors and corrupted scaffold files ([cba4033](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cba4033054758eabc5c1385bf6b6fa16a7d61967))
* repair syntax errors and lint failures blocking CI ([83bacd3](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/83bacd35c263ff48fb07e217d8788cac247503c8))
* Resolve compilation errors in zk.rs for ark-ff 0.4 compatibility ([f641da1](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/f641da17e14ab4d0eab0e7406ef93ce79f8ff3df))


### Documentation

* **rust-sdk:** document MSRV in crates/ledgerlens-sdk/README.md ([#787](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/787)) ([1d2f2df](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/1d2f2dfe61e60132f443fb68d4510cb960c0a57e))
* **rust-sdk:** document MSRV in crates/ledgerlens-sdk/README.md ([#787](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/787)) ([e50deff](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e50deffb196571e2cf5402ea8ba7121a2a4c3b57))
* **sdk:** add CHANGELOG.md and link from README and Cargo.toml ([d152136](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/d152136841954a2e3df2fe77876e265bd7bb01a5))
* **sdk:** add CHANGELOG.md and link from README and Cargo.toml ([7f9b7d0](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/7f9b7d01768d73c2d209e59a7871991f4fd8e5d8))
* **sdk:** add rustdoc examples to public API items ([fce0b19](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/fce0b1984b272f79c7bd9f2ebb99ea1915250f3e))


### Styling

* satisfy rustfmt and remove one more unused import ([8fcce87](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8fcce87ad66a7385762b2e5c98aabbdfc63c4c6b))


### Code Refactoring

* **sdk:** dedupe reqwest client builder; docs(go): document minimum Go version ([6545377](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/65453774dfbaadb870e79737deaa2ffd9c43cfa6)), closes [#784](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/784) [#783](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/783)
* **sdk:** dedupe reqwest Client construction in LedgerLensClient ([e888605](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e88860597fd131a28c31a0f708ed07a4fdff8213))
* **sdk:** dedupe reqwest Client construction in LedgerLensClient ([e650097](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e6500976cb0b9b4c03b9de486732101834f351d7))

## [Unreleased]

### Added

- Documented semver policy and release process in the README.
- API contract tests (`tests/api_contract_test.rs`) validating `RiskScore`
  against `docs/openapi.json`, run in CI.

## [0.1.0] - 2024-01-01

### Added

- Initial `LedgerLensClient` HTTP client with `get_score`, `get_scores`, `get_rings`, and `health` methods
- Typed response models: `RiskScore`, `WalletScoresResponse`, `Ring`, `HealthStatus`, `CrossChainLink`
- `LedgerLensError` enum covering HTTP, API, auth, rate-limit, and deserialization errors
- Optional `zk-verify` feature: `verify_threshold_proof` reimplementation using `ark-bn254`
- `danger_accept_invalid_certs` constructor for local testing
- API key redaction in `Debug` output

### Fixed

- Compilation errors in `zk.rs` for `ark-ff 0.4` API compatibility
- CI workflow failures

[Unreleased]: https://github.com/Derry255/Ledgerlens-core/compare/ledgerlens-sdk-v0.1.0...HEAD
[0.1.0]: https://github.com/Derry255/Ledgerlens-core/releases/tag/ledgerlens-sdk-v0.1.0
