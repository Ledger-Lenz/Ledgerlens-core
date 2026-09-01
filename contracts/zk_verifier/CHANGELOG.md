# Changelog

All notable changes to the `zk_verifier` contract are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed
- Made the contract compile and actually verify proofs.
- Resolved build errors by adding Fq::is_valid and fixing from_bytes conversions.
- Pinned ed25519-dalek/rand/rand_core versions in contract manifests for fuzz build.
- Enabled testutils feature and fixed harness types for fuzzing.

### Added
- Implemented zero-knowledge risk score proofs.
- Built a fuzzing and symbolic-execution harness for the Soroban contract.
- Added zk-SNARK backend.
