# ZK-SNARK Range Proof Backend

This document details the zk-SNARK (Groth16 on BN254) alternative backend for risk score range proofs.

## Circuit Design

The Circom circuit is located in `circuits/score_range_proof.circom`. It proves that a private risk score satisfies:
1. **Pedersen Commitment Binding:** $C = score \cdot G + blinding \cdot H$ matches the public coordinates `commitX` and `commitY` on the BN254 G1 curve.
2. **Range Check:** $0 \leq score \leq 100$ via `Num2Bits(7)`.
3. **Threshold Enforcement:** $score \geq threshold$ via `GreaterEqThan(8)`.

The circuit performs native elliptic curve addition using precomputed powers-of-two coordinates of base points $G$ and $H$. This keeps the constraint count minimal and verification gas cost extremely low.

## Trusted Setup Ceremony

The Groth16 zk-SNARK requires a trusted setup ceremony split into two phases:
1. **Phase 1 (Powers of Tau):** Uses a public universal SRS ceremony transcript (e.g. Perpetual Powers of Tau / Hermez Phase 1) containing $2^{15}$ constraints or more.
2. **Phase 2 (Circuit Specific):** A multi-party computation (MPC) ceremony with at least 3 independent contributors contributing entropy sequentially to generate the proving key (`.zkey`) and verification key (`verification_key.json`).

The generated ceremony transcript and contribution logs are stored at `docs/ceremony_transcripts/`.

### Key Rotation & Integrity

Proving/verification key files are checksummed using SHA-256. The checksums are verified before proof generation or verification to prevent unauthorized key modifications.

```txt
SHA-256 Checksums:
circuits/keys/score_range_proof.zkey:  [hash]
circuits/keys/verification_key.json:  [hash]
```

## Comparison: Sigma Protocol vs. zk-SNARK

| Metric | Sigma Protocol (Default) | zk-SNARK (Alternative) |
| :--- | :--- | :--- |
| **Proof Size** | Scales linearly with `NUM_BITS` (~1.5 KB for 7 bits) | Constant size (~256 bytes) |
| **Prover Time** | Very fast off-chain (~10 ms) | Moderate off-chain (~300-500 ms) |
| **Verifier Cost** | High gas (replays EC arithmetic per bit) | Low gas (fixed-cost Groth16 pairing check) |
| **Trust Assumption** | Setup-free (discrete-log hardness) | Trusted setup (Groth16 Phase-2 ceremony) |

## Key Rotation Procedure

To rotate the proving/verification keys:
1. Initialize Phase 2 contribution using the setup script: `scripts/setup_trusted_ceremony.sh`.
2. Generate the new `.zkey` and `verification_key.json`.
3. Calculate the new SHA-256 checksums and update the settings configurations.
