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

## Negative Test Vectors

The circuit test suite (`circuits/test/score_range_proof.test.js`) includes negative test vectors that **must fail** proof generation or verification. These guard against under-constrained signals that would otherwise let an invalid witness produce a valid proof.

| Vector | Input | Expected result |
| --- | --- | --- |
| Out-of-range (high) | `score = 101` | Proof generation fails |
| Out-of-range (low) | `score = -1` (field element `p - 1`) | Proof generation fails |
| Boundary (valid) | `score = 0` | Proof verifies |
| Boundary (valid) | `score = 100` | Proof verifies |
| Boundary (invalid) | `score = 100`, `threshold = 101` | Proof generation fails |
| Malformed witness | `score` not matching the public commitment | Verification fails |
| Malformed witness | Missing / zeroed private input | Proof generation fails |

Boundary values `0` and `100` are the only accepted extremes; any value outside `[0, 100]` must be rejected by the range constraints.

## Constraint-Count & Under-Constrained-Signal Audit

We run a static audit of `score_range_proof.circom` on every CI run using [circomspect](https://github.com/trailofbits/circomspect) (or an equivalent circom analyzer). The audit:

1. Compiles the circuit and records the **expected constraint count**.
2. Flags any **under-constrained signals** (signals that appear in the witness but are not fully constrained).
3. Fails the build if the constraint count deviates from the recorded baseline or if a new under-constrained signal is reported.

### Expected constraint count

The baseline constraint count for `score_range_proof.circom` is recorded in `circuits/constraint_baseline.json`. CI compares the freshly compiled count against this baseline and **fails on any deviation**, so a change that adds or removes constraints must update the baseline explicitly (making the change reviewable).

### Running the audit locally

```sh
# Compile and count constraints
circom circuits/score_range_proof.circom --r1cs --wasm -o build/
snarkjs r1cs info build/score_range_proof.r1cs

# Static analysis for under-constrained signals
circomspect circuits/score_range_proof.circom
```

The same commands run in CI as a **required check** (`.github/workflows/circuit-audit.yml`); the build fails on new under-constrained signals or a constraint-count deviation.

### Methodology

- **Negative vectors** exercise the range and threshold constraints with out-of-range, boundary, and malformed witnesses; each must fail as documented above.
- **Constraint count** is treated as a regression signal: an unexpected change usually means a constraint was added or dropped, which is exactly where under-constraining bugs hide.
- **Under-constrained-signal analysis** (circomspect) is the primary detector for missing constraints; its findings are blocking, not advisory.
