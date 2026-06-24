---
title: "Build Multi-Signature Oracle Quorum for Tamper-Resistant On-Chain Score Publication"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/oracle_node.py` and `contracts/oracle_aggregator/src/lib.rs` to implement a 3-of-5 multi-oracle quorum: score submissions to the Soroban on-chain registry require threshold signatures from at least 3 of 5 independent oracle nodes before being accepted on-chain. Implement ED25519 multi-sig aggregation in the oracle coordinator, and a Soroban contract (`oracle_aggregator`) that verifies the quorum signature before calling `submit_score` on the main `ledgerlens-score` contract. This removes the single point of trust in the current single-key `SorobanPublisher`.

## Background & Context

The current `SorobanPublisher` uses a single service account keypair (`LEDGERLENS_SERVICE_SECRET_KEY`) to sign and submit all on-chain score updates. This is a single point of failure and trust: if the service key is compromised, an attacker can submit arbitrary risk scores on-chain, potentially extorting wallets or clearing genuine fraud flags.

A multi-oracle quorum architecture distributes this trust across `n=5` independent oracle nodes, each holding its own ED25519 keypair. A score submission is only accepted on-chain when `k=3` oracles have independently computed the same score and signed the submission. The Soroban `oracle_aggregator` contract verifies that the collected signatures form a valid `k-of-n` threshold before forwarding to `ledgerlens-score`.

This architecture is analogous to a Stellar multi-sig account but implemented at the application layer using Soroban contracts, which gives more flexibility for threshold changes and key rotation.

The quorum also protects against oracle failures: the system remains operational as long as ≥3 of 5 oracles are online. With `k=3, n=5`, the system tolerates 2 simultaneous oracle failures.

## Objectives

- [ ] Implement `OracleNode` class in `detection/oracle_node.py` encapsulating an ED25519 keypair, a method `sign_score_submission(wallet, asset_pair, score, timestamp) -> bytes`, and a `public_key_hex` property.
- [ ] Implement `OracleCoordinator` in `detection/oracle_coordinator.py` that collects signatures from multiple `OracleNode` instances and assembles a `QuorumSignature` when ≥k signatures are received.
- [ ] `QuorumSignature` includes: `message_bytes`, list of `(public_key_hex, signature_hex)` pairs, `signers_count`, and `threshold`.
- [ ] Implement `OracleCoordinator.submit_with_quorum(wallet, asset_pair, score, timestamp)` that gathers signatures from all configured oracle nodes and calls the `oracle_aggregator` Soroban contract only when ≥k signatures are collected.
- [ ] Implement the `oracle_aggregator` Soroban contract in Rust (`contracts/oracle_aggregator/src/lib.rs`) with function `submit_with_quorum(wallet, asset_pair, score, timestamp, signatures: Vec<(BytesN<32>, BytesN<64>)>) -> bool`.
- [ ] The Soroban contract verifies each ED25519 signature against the message (canonical serialisation of `wallet||asset_pair||score||timestamp`), counts valid signatures, and accepts if count ≥ `THRESHOLD` (stored in contract storage, initialised at deploy time).
- [ ] Implement `GET /admin/oracle/status` endpoint returning each oracle node's name, public key, and last-seen timestamp.
- [ ] All oracle private keys loaded from environment variables only; never written to disk or logged.
- [ ] Write unit tests for signature aggregation and threshold verification.

## Technical Requirements

### `OracleNode` (`detection/oracle_node.py`)

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import os

class OracleNode:
    def __init__(self, name: str, private_key_env_var: str):
        """
        Load ED25519 private key from environment variable (32 hex-encoded bytes).
        Raises EnvironmentError if the variable is not set.
        """
        raw = os.environ.get(private_key_env_var)
        if not raw:
            raise EnvironmentError(f"Oracle key not set: {private_key_env_var}")
        key_bytes = bytes.fromhex(raw)
        self._private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
        self.name = name

    @property
    def public_key_hex(self) -> str:
        pub = self._private_key.public_key()
        return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign_score_submission(
        self, wallet: str, asset_pair: str, score: int, timestamp: int
    ) -> bytes:
        """
        Sign canonical message: SHA-256("LedgerLens-Oracle-v1" || wallet || asset_pair || score_u32_be || timestamp_u64_be)
        Returns 64-byte ED25519 signature.
        """
        message = self._canonical_message(wallet, asset_pair, score, timestamp)
        return self._private_key.sign(message)

    @staticmethod
    def _canonical_message(wallet: str, asset_pair: str, score: int, timestamp: int) -> bytes:
        import hashlib, struct
        prefix = b"LedgerLens-Oracle-v1"
        body = (
            prefix
            + wallet.encode("utf-8")
            + b"|"
            + asset_pair.encode("utf-8")
            + b"|"
            + struct.pack(">I", score)
            + struct.pack(">Q", timestamp)
        )
        return hashlib.sha256(body).digest()
```

### `OracleCoordinator` (`detection/oracle_coordinator.py`)

```python
from dataclasses import dataclass
from typing import List, Tuple

@dataclass
class QuorumSignature:
    message_bytes: bytes            # canonical message that was signed
    signatures: List[Tuple[str, str]]  # [(public_key_hex, signature_hex), ...]
    signers_count: int
    threshold: int
    is_valid_quorum: bool           # True if signers_count >= threshold

class OracleCoordinator:
    def __init__(self, nodes: List[OracleNode], threshold: int = 3):
        if threshold > len(nodes):
            raise ValueError(f"Threshold {threshold} > node count {len(nodes)}")
        self.nodes = nodes
        self.threshold = threshold

    def collect_signatures(
        self, wallet: str, asset_pair: str, score: int, timestamp: int
    ) -> QuorumSignature:
        """Collect signatures from all nodes; stop after threshold is reached."""
        message = OracleNode._canonical_message(wallet, asset_pair, score, timestamp)
        signatures = []
        for node in self.nodes:
            try:
                sig = node.sign_score_submission(wallet, asset_pair, score, timestamp)
                signatures.append((node.public_key_hex, sig.hex()))
                if len(signatures) >= self.threshold:
                    break      # Short-circuit: quorum reached
            except Exception as e:
                logger.warning("Oracle %s failed to sign: %s", node.name, e)
        return QuorumSignature(
            message_bytes=message,
            signatures=signatures,
            signers_count=len(signatures),
            threshold=self.threshold,
            is_valid_quorum=len(signatures) >= self.threshold,
        )

    def submit_with_quorum(
        self, wallet: str, asset_pair: str, score: int, timestamp: int,
        publisher: "SorobanPublisher"
    ) -> bool:
        quorum = self.collect_signatures(wallet, asset_pair, score, timestamp)
        if not quorum.is_valid_quorum:
            logger.error("Quorum not reached: %d/%d signatures", quorum.signers_count, self.threshold)
            return False
        # Call oracle_aggregator Soroban contract
        return publisher.submit_with_quorum(wallet, asset_pair, score, timestamp, quorum)
```

### Soroban `oracle_aggregator` contract (`contracts/oracle_aggregator/src/lib.rs`)

```rust
use soroban_sdk::{contract, contractimpl, contracttype, Address, BytesN, Env, Symbol, Vec};

#[contracttype]
pub struct SignaturePair {
    pub public_key: BytesN<32>,
    pub signature: BytesN<64>,
}

#[contract]
pub struct OracleAggregator;

#[contractimpl]
impl OracleAggregator {
    /// Initialise with threshold k and list of n authorised oracle public keys.
    pub fn initialize(env: Env, threshold: u32, oracle_keys: Vec<BytesN<32>>) { ... }

    /// Verify k-of-n signatures and forward to ledgerlens-score contract.
    pub fn submit_with_quorum(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        timestamp: u64,
        signatures: Vec<SignaturePair>,
    ) -> bool {
        let threshold: u32 = env.storage().instance().get(&Symbol::new(&env, "THRESHOLD")).unwrap();
        let oracle_keys: Vec<BytesN<32>> = env.storage().instance().get(&Symbol::new(&env, "ORACLE_KEYS")).unwrap();
        
        let message = Self::canonical_message(&env, &wallet, &asset_pair, score, timestamp);
        let mut valid_count: u32 = 0;
        
        for sig_pair in signatures.iter() {
            if oracle_keys.contains(&sig_pair.public_key) {
                if env.crypto().ed25519_verify(&sig_pair.public_key, &message, &sig_pair.signature).is_ok() {
                    valid_count += 1;
                }
            }
        }
        
        if valid_count < threshold {
            return false;
        }
        // Forward to ledgerlens-score contract
        let score_contract: Address = env.storage().instance().get(&Symbol::new(&env, "SCORE_CONTRACT")).unwrap();
        // invoke submit_score on ledgerlens-score
        true
    }
    
    fn canonical_message(env: &Env, wallet: &Address, asset_pair: &Symbol, score: u32, timestamp: u64) -> soroban_sdk::Bytes {
        // Matches Python OracleNode._canonical_message exactly
        ...
    }
}
```

### Configuration (`.env.example`)

```
ORACLE_NODE_1_KEY=<hex-encoded 32-byte ED25519 private key>
ORACLE_NODE_2_KEY=<hex-encoded 32-byte ED25519 private key>
ORACLE_NODE_3_KEY=<hex-encoded 32-byte ED25519 private key>
ORACLE_NODE_4_KEY=<hex-encoded 32-byte ED25519 private key>
ORACLE_NODE_5_KEY=<hex-encoded 32-byte ED25519 private key>
ORACLE_QUORUM_THRESHOLD=3
```

## Security Considerations

- **Private key isolation**: each oracle node's key must be in a separate environment variable. Never log any oracle private key or expose it through the `/admin/oracle/status` endpoint (which shows public keys only).
- **Message canonicalisation must match exactly between Python and Rust**: any discrepancy in byte encoding causes signature verification failures. Use identical domain separator (`"LedgerLens-Oracle-v1"`), field ordering, and byte packing in both implementations. Cover this with a cross-language test vector.
- **Replay protection**: the canonical message includes `timestamp`; the Soroban contract should reject timestamps older than 5 minutes (using `env.ledger().timestamp()`).
- **Key rotation**: oracle key rotation requires a contract re-initialisation (`initialize` call with new key set). Document this procedure in `docs/oracle_operations.md`.
- **Threshold reduction attack**: ensure `initialize` can only be called once or by an authorised admin address (Stellar account stored in contract storage at first init). Subsequent key set changes require a governance proposal (see ISSUE-070).

## Testing Requirements

- **Unit — `OracleNode.sign_score_submission`**: assert signature is 64 bytes; assert verification with public key succeeds.
- **Unit — canonical message consistency**: Python and a test vector from the Rust implementation must produce the same 32-byte SHA-256 digest for identical inputs.
- **Unit — `OracleCoordinator.collect_signatures` short-circuit**: 5 nodes available, threshold=3; assert only 3 node sign() calls are made.
- **Unit — quorum failure tolerance**: mock 2 nodes failing; assert quorum still reached with 3 remaining.
- **Unit — quorum not reached**: mock 3 of 5 nodes failing; assert `is_valid_quorum=False`.
- **Unit — `submit_with_quorum` returns False on quorum failure**: assert Soroban publisher is never called.
- **Rust unit test — `submit_with_quorum` accepts valid k-of-n**: construct 3 valid signatures from known test keys; assert returns True.
- **Rust unit test — rejects n-1 signatures**: 2 valid signatures, threshold=3; assert returns False.
- **Rust unit test — rejects forged signature**: replace one valid signature with random bytes; assert returns False.
- **Rust unit test — rejects unknown oracle key**: submit signature from a key not in the oracle key list; assert it is not counted.

## Documentation Requirements

- Docstrings on `OracleNode`, `OracleCoordinator`, and all public methods.
- New file `docs/oracle_quorum.md` covering: architecture overview, key management, threshold selection guidance, key rotation procedure, and failure mode analysis.
- Update `README.md` Soroban Integration section to describe the quorum architecture.
- Document `ORACLE_NODE_*_KEY` variables in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `OracleNode` implemented with correct ED25519 signing and canonical message construction.
- [ ] `OracleCoordinator` collects signatures with short-circuit and quorum validation.
- [ ] `oracle_aggregator` Soroban contract verifies k-of-n ED25519 signatures and forwards to `ledgerlens-score`.
- [ ] Canonical message format is identical between Python and Rust (verified by shared test vector).
- [ ] Replay protection (timestamp check) implemented in Soroban contract.
- [ ] `GET /admin/oracle/status` shows public keys and last-seen timestamps.
- [ ] All Python unit tests pass; all Rust unit tests pass (`cargo test`).
- [ ] Oracle private keys never appear in logs or API responses.
- [ ] `docs/oracle_quorum.md` written.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience building threshold signature systems or multi-party signing protocols, ideally in a blockchain context. You are comfortable with ED25519 signing in Python (`cryptography` library) and Rust (`soroban_sdk` / `ed25519-dalek`). Understanding the Soroban smart contract execution model and cross-contract invocation patterns is required for the Rust contract work. Experience with Stellar's multi-sig accounts or other threshold cryptography schemes will translate well.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., threshold cryptography, Soroban/Rust smart contracts, Python cryptography).
2. **Relevant experience**: multi-sig or threshold signing implementations; Soroban contract work; links to code appreciated.
3. **Approach / thoughts**: would you implement the aggregation as a Schnorr multi-sig (single aggregate signature) rather than independent ED25519 signatures? What are the tradeoffs for Soroban verification gas cost?
4. **Estimated time**: realistic estimate to complete Python implementation, Rust contract, tests, and documentation.
