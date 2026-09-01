# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
