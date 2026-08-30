# Changelog

All notable changes to the oracle_aggregator contract are documented here.
Format based on Keep a Changelog (https://keepachangelog.com/en/1.0.0/).

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
