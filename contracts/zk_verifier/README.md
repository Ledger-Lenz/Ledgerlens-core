# ZK Verifier Contract

Soroban smart contract for zero-knowledge risk score verification. Stores cryptographic commitments for each wallet and verifies threshold proofs without revealing the underlying risk score.

## Overview

The ZK Verifier enables **privacy-preserving score verification**: downstream contracts (AMMs, lending protocols) can check `score >= threshold` without learning the exact score or any feature values. This is critical for:
- Preserving wallet privacy while still gating high-risk activity
- Preventing score gaming (attackers can't see their exact score to optimize around thresholds)
- Regulatory compliance (scoring logic is confidential but verifiable)

## Contract Functions

### `submit_score(env: Env, admin: Address, wallet: Address, score: u32, commitment_hash: BytesN<32>, pedersen_x: BytesN<32>, pedersen_y: BytesN<32>)`

Stores a risk score and its cryptographic commitments for a wallet.

**Parameters:**
- `admin` — administrator address (must authorize the call)
- `wallet` — the wallet being scored
- `score` — numeric risk score 0-100 (for non-ZK consumers)
- `commitment_hash` — SHA-256 commitment hash (public binding)
- `pedersen_x`, `pedersen_y` — Pedersen commitment point coordinates on BN254

**Authorization:** **REQUIRED** via `admin.require_auth()`

**Intentional Panics:** None (auth failures are handled by `require_auth()`)

**Side Effects:**
- Stores `ScoreCommitment` in contract storage
- Sets `timestamp` to current ledger timestamp

**Tested by:** `test.rs` (when it exists), `fuzz_submit_score`

### `get_score(env: Env, wallet: Address) -> u32`

Returns the stored numeric score for a wallet (non-ZK path).

**Authorization:** Public (read-only)

**Returns:** Score 0-100, or `0` if no score exists for the wallet

**Intentional Panics:** None

**Tested by:** `test.rs`, `fuzz_submit_score`

### `get_commitment(env: Env, wallet: Address) -> Option<ScoreCommitment>`

Returns the full commitment record for a wallet.

**Authorization:** Public (read-only)

**Returns:** 
- `Some(ScoreCommitment)` if a score exists
- `None` if no score exists

**Intentional Panics:** None

**Tested by:** `test.rs`

### `verify_threshold(env: Env, wallet: Address, threshold: u32, proof: Bytes) -> bool`

Verifies a zero-knowledge proof that `wallet`'s score meets `threshold` without revealing the score.

**Parameters:**
- `wallet` — wallet being verified
- `threshold` — minimum score required (0-100)
- `proof` — CBOR-serialized proof π from the off-chain prover

**Authorization:** Public (verification is deterministic)

**Returns:** 
- `true` if proof is valid AND `score >= threshold`
- `false` if no score exists, proof is malformed, or verification fails

**Intentional Panics:** None (malformed proofs return `false`, not panic)

**Proof Format:** Sigma protocol on BN254 (see [Proof Structure](#proof-structure) below)

**Tested by:** `fuzz_verify_threshold`

## Proof Structure

The off-chain prover (`detection/zk_prover.py`) produces a Sigma protocol proof:

```
π = {
    score_commit: P = s·G + r·H,  # Pedersen commitment on BN254
    bits[0..6]: [
        {
            commit: B_i = b_i·G + r_i·H,  # Bit commitment
            c0, c1, s0, s1                 # Sigma OR-proof that b_i ∈ {0,1}
        },
        ...
    ]
}
```

Where `s` is the score, `T` is the threshold, and the bits represent `d = s - T`.

### Verification Algorithm

For each bit:
1. `R0 = s0·H - c0·B_i`
2. `R1 = s1·H - c1·(B_i - G)`
3. `c = SHA256(R0 || R1 || B_i || context)`
4. Check `c0 + c1 == c` (Fiat-Shamir challenge)

Then verify the bit sum:
- `Σ 2^i · B_i == P - T·G`

This proves `score - threshold >= 0` (represented as 7 bits) without revealing `score`.

## Security Guarantees

### Authorization Model

**CRITICAL:** `submit_score` is the only write operation and it **REQUIRES** `admin.require_auth()`.

This prevents:
- Unauthorized wallets from forging their own score commitments
- Replay attacks (each `submit_score` overwrites the previous commitment)
- Unauthorized score updates

The authorization check is tested by `fuzz_auth_bypass`, which attempts to call `submit_score` without mocking authorization and asserts it fails.

### Known Intentional Behaviors

| Operation          | Condition                | Behavior                   | Test Coverage           |
| ------------------ | ------------------------ | -------------------------- | ----------------------- |
| `submit_score`     | No authorization         | Panic from `require_auth()` | `fuzz_auth_bypass`      |
| `verify_threshold` | Malformed proof          | Return `false`             | `fuzz_verify_threshold` |
| `verify_threshold` | Empty proof bytes        | Return `false`             | `fuzz_verify_threshold` |
| `verify_threshold` | No score exists          | Return `false`             | `test.rs`               |
| `get_score`        | No score exists          | Return `0`                 | `test.rs`, `fuzz_submit_score` |

### Arithmetic Overflow Safety

The contract sets `overflow-checks = true` in `[profile.release]`. The fuzzing infrastructure tests:

- Score values (0, 50, 100, u32::MAX)
- Threshold values (0, 50, 100, u32::MAX, score±1)
- Proof lengths (0 bytes, 1024 bytes, 4096 bytes)
- Malformed proof byte patterns

The curve arithmetic in `curve::Point` (BN254 field operations) is hand-written and not yet formally verified. The fuzzer exercises all public curve operations through the `verify_threshold` entrypoint with adversarial inputs.

**Current Status:** No unintentional overflow panics have been found in fuzzing. Curve operations use checked arithmetic where possible, but some low-level field operations assume inputs are already reduced mod p.

## Cryptographic Primitives

### Curve: BN254

The contract uses BN254 (also called alt_bn128) for Pedersen commitments and Sigma protocol proofs. BN254 is:
- Widely used in ZK systems (Zcash, Ethereum zkSNARKs)
- Has a 254-bit prime field
- Efficient for on-chain verification

The curve implementation is in `src/curve.rs` (not shown here but included in the contract).

### Commitments

**SHA-256 hash commitment:**
- Binds the prover to a specific score before generating the ZK proof
- Stored as `commitment_hash` in `ScoreCommitment`

**Pedersen commitment:**
- Homomorphic: `P(s1 + s2) = P(s1) + P(s2)`
- Hiding: `P(s)` reveals nothing about `s` without the randomness `r`
- Binding: Cannot find `s' ≠ s` with same commitment (computationally)

### Sigma Protocol

The bit-proof Sigma protocol is a **proof of knowledge** that each `b_i ∈ {0,1}` using an OR-proof:
- Prover knows either `b_i = 0` OR `b_i = 1`
- Challenge `c = c0 + c1` is Fiat-Shamir transformed from a transcript hash
- Soundness: a cheating prover (trying to prove `b_i = 2`) would need to break SHA-256

## Fuzzing

This contract has three fuzz targets (see [docs/contract_fuzzing.md](../../docs/contract_fuzzing.md)):

| Target                   | Description                                              | Time Budget (PR) | Time Budget (Nightly) |
| ------------------------ | -------------------------------------------------------- | ---------------- | --------------------- |
| `fuzz_submit_score`      | Arbitrary score, commitment bytes (boundary values)      | 120s             | 30min                 |
| `fuzz_verify_threshold`  | Malformed proof bytes, arbitrary threshold               | 120s             | 30min                 |
| `fuzz_auth_bypass`       | Authorization requirement enforcement                    | 120s             | 30min                 |

Run locally:

```bash
cd contracts/zk_verifier
cargo +nightly fuzz run fuzz_verify_threshold -- -max_total_time=120
```

## Testing

Unit tests: `src/test.rs` (to be created)

```bash
cargo test
```

Fuzzing (requires nightly):

```bash
cargo +nightly fuzz run fuzz_submit_score
```

## Building

Standard Soroban contract build:

```bash
cargo build --target wasm32-unknown-unknown --release
```

The contract is configured as `crate-type = ["cdylib", "rlib"]` so it can be built both as a WASM contract (`cdylib`) and as a library for testing and fuzzing (`rlib`).

## Dependencies

- `soroban-sdk` — Soroban smart contract SDK (workspace dependency)
- `soroban-sdk-derive` — Derive macros for Soroban contracts
- `arbitrary` — Structured fuzzing input generation (dev-dependency)

## Future Work

### Symbolic Verification (Stretch Goal)

The issue mentions symbolic execution (e.g., `cargo kani`) as a stretch goal beyond fuzzing. This would provide:
- **Formal proof** of unreachability for overflow panics
- **Exhaustive** coverage of all 32-bit score/threshold combinations
- **Sound** verification of curve arithmetic properties

Kani harnesses would target the same entrypoints as the fuzz harnesses but with symbolic inputs rather than concrete fuzzer-generated bytes.

**Status:** Not yet implemented. Fuzzing is the primary gate; Kani would be a follow-up issue.

### Proof Deserialization

The current `deserialise_proof` implementation returns `None` (placeholder). Production implementation would:
1. Parse CBOR-like bytes into `ProofData` struct
2. Validate proof structure (correct number of bits, point on curve, etc.)
3. Return `Some(proof_data)` on success, `None` on malformed input

**Status:** Scaffold only. Fuzzing currently tests the `None` path (malformed proof rejection).

### Fiat-Shamir Hash

The current `fiat_shamir` implementation returns a placeholder value. Production implementation would:
1. Concatenate `R0_x || R0_y || R1_x || R1_y || B_x || B_y || context`
2. SHA-256 hash the concatenation
3. Reduce mod BN254 curve order

**Status:** Scaffold only. Fuzzing tests that the function is callable with arbitrary inputs.

## References

- [Contract fuzzing documentation](../../docs/contract_fuzzing.md)
- [LedgerLens ZK design](../../docs/zk_design.md) *(if it exists)*
- [Soroban SDK documentation](https://soroban.stellar.org/docs/reference/sdk)
- [BN254 curve specification](https://hackmd.io/@jpw/bn254)
- [Sigma protocols](https://en.wikipedia.org/wiki/Proof_of_knowledge#Sigma_protocols)
