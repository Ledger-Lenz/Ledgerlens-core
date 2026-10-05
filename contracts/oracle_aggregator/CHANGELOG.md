# Changelog

All notable changes to the oracle_aggregator contract are documented here.
Format based on Keep a Changelog (https://keepachangelog.com/en/1.0.0/).

## [0.2.0](https://github.com/Ledger-Lenz/Ledgerlens-core/compare/oracle-aggregator-v0.1.0...oracle-aggregator-v0.2.0) (2026-10-05)


### Features

* harden model loading, contract fuzz gate, SLO alerts, and metric cardinality ([#1077](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1077)) ([301afc8](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/301afc8a5232ee8ba3238b7efce37b08a5b9bb30)), closes [#1000](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1000) [#1001](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1001) [#1002](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1002) [#1003](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1003)


### Bug Fixes

* **contracts:** make both Soroban crates build and test again ([c1ad377](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c1ad377f47b4c102df0eb5a2bb6fd1f295352b36))
* **oracle:** require auth on OracleAggregator::initialize (issue [#688](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/688)) ([05cd9cd](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/05cd9cd1801c95b90d74df7e04624f35603ede7f))
* **oracle:** require auth on OracleAggregator::initialize (issue [#688](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/688)) ([3c3f49e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3c3f49e1f8124dd3bb3620c00cb648e986b38fda))
* repair CI-breaking compile errors and corrupted scaffold files ([cba4033](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/cba4033054758eabc5c1385bf6b6fa16a7d61967))
* wire quorum scores to registry ([#684](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/684)) ([6030f39](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/6030f39306cfce32693b14970d9d04f768ba7563))


### Documentation

* document Docker build steps, panic messages, and add CHANGELOGs ([#791](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/791), [#792](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/792), [#793](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/793), [#794](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/794)) ([3325df4](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/3325df4c536bb183e36b8881d522b73277915d07))
* document Docker build steps, panic messages, and add CHANGELOGs… ([c229d88](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/c229d882a2848eef62b5201b43170491a5f0179e))


### CI

* build and test both Soroban contract crates on every push and PR ([a481cbf](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/a481cbf5afc3a187dfc85ae70288acec25716f6a))

## [Unreleased]

### Fixed
- Unblocked the fuzz CI job so zk_verifier's fuzz targets actually run.
- Made the contract compile; fixed quorum bypass and wire format issues.
- Enabled testutils feature and fixed asset_pair type in fuzz harnesses.
- Pinned ed25519-dalek/rand/rand_core versions in contract manifests for fuzz build.
- Resolved repo-wide lint errors and regenerated OpenAPI schema.

### Added
- Built a fuzzing and symbolic-execution harness for the Soroban contract.
- Implemented multi-signature oracle quorum for tamper-resistant on-chain score publication.
- Initial contract scaffold for oracle network feature.
