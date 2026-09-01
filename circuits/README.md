# ZK Score Range Proof Circuits

Circom circuits for zero-knowledge range proofs of risk scores.

## Overview

This directory contains the cryptographic circuit definitions for proving that a wallet's risk score satisfies specific properties (in range 0–100, meets a threshold) without revealing the score itself.

## Files

- **`score_range_proof.circom`** — Main circuit proving:
  - Score is in the range [0, 100]
  - Score meets a given threshold
  - Public Pedersen commitment binds the prover to a specific score
  
- **`constants.circom`** — Shared constants and helper definitions

## Related Resources

- **Soroban Verifier Contract:** [contracts/zk_verifier/README.md](../contracts/zk_verifier/README.md)  
  The on-chain contract that verifies proofs generated from these circuits (Sigma protocol proof variant).

- **zk-SNARK Range Proof Backend:** [docs/zk_snark_range_proof.md](../docs/zk_snark_range_proof.md)  
  Conceptual documentation of the zk-SNARK backend (Groth16 alternative using these circuits), trusted setup, and key rotation procedures.

## Integration

The circuits are used in two proof systems:

1. **Sigma Protocol (Default):** Fast off-chain proof generation; higher on-chain verification gas cost. Implemented in the Soroban contract.
2. **zk-SNARK (Groth16):** Slower off-chain proof generation; constant-size proofs and low-gas on-chain verification. Uses this circuit with a trusted setup ceremony.

For details on comparing the two approaches, see the table in [docs/zk_snark_range_proof.md](../docs/zk_snark_range_proof.md#comparison-sigma-protocol-vs-zk-snark).
