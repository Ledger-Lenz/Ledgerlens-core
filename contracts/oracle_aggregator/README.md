# Oracle Aggregator Contract

Soroban smart contract for the LedgerLens oracle network. Implements k-of-n quorum signature verification for off-chain oracle nodes submitting risk scores on-chain.

## Overview

The Oracle Aggregator is a critical trust boundary component that:
- Verifies Ed25519 signatures from authorized oracle nodes
- Enforces a configurable threshold (k-of-n) quorum before accepting scores
- Forwards validated scores to the main LedgerLens score contract
- Implements replay protection via timestamp validation

## Contract Functions

### `initialize(env: Env, threshold: u32, oracle_keys: Vec<BytesN<32>>, score_contract: Address)`

Initializes the contract with:
- `threshold` — minimum number of valid signatures required (k in k-of-n)
- `oracle_keys` — vector of authorized oracle Ed25519 public keys (n oracles total)
- `score_contract` — address of the LedgerLens score contract to forward to

**Authorization:** No auth required (first-write wins)

**Intentional Panics:**
- `"already initialized"` — if called more than once (prevents re-initialization attacks)

**Tested by:** `test.rs::test_double_initialization_fails`, `fuzz_auth_bypass`

### `submit_with_quorum(env: Env, wallet: Address, asset_pair: Symbol, score: u32, timestamp: u64, signatures: Vec<SignaturePair>) -> bool`

Verifies k-of-n signatures and returns `true` if quorum is reached.

**Parameters:**
- `wallet` — the wallet being scored
- `asset_pair` — trading pair symbol (e.g., `"XLM-USDC"`)
- `score` — risk score 0-100
- `timestamp` — Unix timestamp of the score computation
- `signatures` — vector of `SignaturePair { public_key, signature }`

**Authorization:** No auth required (signature-based verification instead)

**Validation:**
1. Reject timestamps older than 5 minutes (replay protection)
2. Verify each signature against `canonical_message`
3. Count only signatures from keys in `oracle_keys`
4. Return `true` if `valid_count >= threshold`, else `false`

**Intentional Panics:** None (returns `false` on validation failure)

**Returns:** `true` if quorum reached, `false` otherwise

**Tested by:** `test.rs::test_submit_with_quorum`, `test.rs::test_rejects_n_minus_1_signatures`, `fuzz_submit_with_quorum`

### `canonical_message(env: &Env, wallet: &Address, asset_pair: &Symbol, score: u32, timestamp: u64) -> Bytes`

Constructs the canonical message that oracle nodes sign. Format:

```
SHA-256("LedgerLens-Oracle-v1" || wallet || "|" || asset_pair || "|" || score_u32_be || timestamp_u64_be)
```

This matches the Python `OracleNode._canonical_message` implementation exactly.

**Authorization:** Public (pure function, no side effects)

**Intentional Panics:** None (pure byte packing, no validation)

**Tested by:** `test.rs::test_canonical_message_boundary_values`, `fuzz_canonical_message`

## Signature Verification Process

Oracle nodes construct the canonical message off-chain:

```python
msg = sha256(b"LedgerLens-Oracle-v1" + wallet.encode() + b"|" + asset_pair.encode() + b"|" + score.to_bytes(4, 'big') + timestamp.to_bytes(8, 'big'))
signature = ed25519_sign(oracle_private_key, msg)
```

The contract then:
1. Reconstructs the same canonical message on-chain
2. Verifies each signature with `env.crypto().ed25519_verify(public_key, message, signature)`
3. Counts valid signatures from authorized keys
4. Accepts the score if count >= threshold

## Security Guarantees

### Replay Protection

The 5-minute timestamp window prevents:
- Old scores from being resubmitted after a wallet's behavior improves
- Replay attacks using captured valid signature sets

### Authorization Model

This contract does NOT use `require_auth()` because:
- Authorization is signature-based, not account-based
- The threshold quorum provides Byzantine fault tolerance
- Off-chain oracle nodes are the trust root, not on-chain accounts

The `initialize` function is protected by a "first-write wins" model — once initialized, the storage slot is set and cannot be overwritten.

### Known Intentional Behaviors

| Operation              | Condition                     | Behavior                                    | Test Coverage                            |
| ---------------------- | ----------------------------- | ------------------------------------------- | ---------------------------------------- |
| `initialize`           | Already initialized           | Panic: `"already initialized"`              | `test.rs`, `fuzz_auth_bypass`            |
| `submit_with_quorum`   | Timestamp too old             | Return `false`                              | `test.rs`                                |
| `submit_with_quorum`   | Insufficient signatures       | Return `false`                              | `test.rs::test_rejects_n_minus_1_signatures` |
| `submit_with_quorum`   | Forged signature              | Return `false`                              | `test.rs::test_rejects_forged_signature` |
| `submit_with_quorum`   | Unknown oracle key            | Return `false`                              | `test.rs::test_rejects_unknown_oracle_key` |
| `canonical_message`    | `u32::MAX`, `u64::MAX` inputs | Return bytes (no panic)                     | `test.rs::test_canonical_message_boundary_values`, `fuzz_canonical_message` |

## Arithmetic Overflow Safety

The contract sets `overflow-checks = true` in `[profile.release]`, which means arithmetic overflow panics rather than wrapping. The fuzzing infrastructure systematically tests:

- Threshold values (0, 1, u32::MAX)
- Oracle key counts (0-20 keys)
- Signature counts (0-25 signatures)
- Score values (0, 100, u32::MAX)
- Timestamp values (0, current, u64::MAX)

No unintentional overflow panics have been found. The contract uses simple counting logic with no complex arithmetic.

## Fuzzing

This contract has three fuzz targets (see [docs/contract_fuzzing.md](../../docs/contract_fuzzing.md)):

| Target                        | Description                                                      | Time Budget (PR) | Time Budget (Nightly) |
| ----------------------------- | ---------------------------------------------------------------- | ---------------- | --------------------- |
| `fuzz_submit_with_quorum`     | Arbitrary threshold, keys, scores, timestamps, signatures        | 120s             | 30min                 |
| `fuzz_canonical_message`      | Arbitrary wallet, asset_pair, score, timestamp (boundary values) | 120s             | 30min                 |
| `fuzz_auth_bypass`            | Double-initialization protection                                 | 120s             | 30min                 |

Run locally:

```bash
cd contracts/oracle_aggregator
cargo +nightly fuzz run fuzz_submit_with_quorum -- -max_total_time=120
```

## Testing

Unit tests: `src/test.rs`

```bash
cargo test
```

Fuzzing (requires nightly):

```bash
cargo +nightly fuzz run fuzz_submit_with_quorum
```

## Building

Standard Soroban contract build:

```bash
cargo build --target wasm32-unknown-unknown --release
```

The contract is configured as `crate-type = ["cdylib", "rlib"]` so it can be built both as a WASM contract (`cdylib`) and as a library for testing and fuzzing (`rlib`).

## Dependencies

- `soroban-sdk` — Soroban smart contract SDK (workspace dependency)
- `ed25519-dalek` — Ed25519 signature verification (dev-dependency, for tests only)
- `rand` — Random number generation (dev-dependency, for test keypair generation)
- `arbitrary` — Structured fuzzing input generation (dev-dependency)

## References

- [LedgerLens Oracle Quorum design](../../docs/oracle_quorum.md)
- [Contract fuzzing documentation](../../docs/contract_fuzzing.md)
- [Soroban SDK documentation](https://soroban.stellar.org/docs/reference/sdk)
