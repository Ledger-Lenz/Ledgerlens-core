# LedgerLens SDK (Rust)

A typed Rust client for the [LedgerLens](https://ledgerlens.io) risk scoring API,
with optional zero-knowledge proof verification for threshold proofs.

## Installation

```toml
[dependencies]
ledgerlens-sdk = "0.1.0"
```

To enable ZK proof verification:

```toml
[dependencies]
ledgerlens-sdk = { version = "0.1.0", features = ["zk-verify"] }
```

## Quick Start (REST API)

```rust
use ledgerlens_sdk::LedgerLensClient;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = LedgerLensClient::new(
        "https://api.ledgerlens.io",
        Some("sk_your_api_key".into()),
    );

    // Fetch scores for a specific wallet
    let scores = client.get_score("GA…").await?;
    println!("{:?}", scores);

    // Fetch all scores
    let all_scores = client.get_scores(None).await?;
    println!("Total scores: {}", all_scores.len());

    // Fetch wash-trading rings
    let rings = client.get_rings().await?;
    println!("Detected {} rings", rings.len());

    // Check API health
    let health = client.health().await?;
    println!("API status: {}", health.status);

    Ok(())
}
```

## Soroban Integration: ZK Proof Verification

For Soroban contract integrators who need to verify a `ThresholdProof` (the
"this wallet's score is below X, without revealing the exact score" proof)
locally before trusting an API response:

```rust,ignore
use ledgerlens_sdk::{verify_threshold_proof, ThresholdProof};

// Deserialize the proof from an API response
let proof: ThresholdProof = serde_json::from_str(response_body)?;

// Verify that the committed score >= 50
let valid = verify_threshold_proof(&proof, 50, "GA…")?;
assert!(valid, "ZK proof verification failed");
```

> **Security**: The ZK verifier is a **pure verifier** — it never requires or
> accepts a secret blinding factor. It reimplements the same Fiat-Shamir
> transcript and BN254 curve arithmetic as the Python prover
> (`detection/zk_prover.py`), using `ark-bn254` as the Rust equivalent of
> `py_ecc.bn128`.

## Feature Flags

| Feature     | Default | Description                                      |
|-------------|---------|--------------------------------------------------|
| `async`     | Yes     | Enable async/await support via `tokio`.          |
| `zk-verify` | No      | Enable ZK threshold proof verification.          |

## API Coverage

| Method | Endpoint | Description |
|--------|----------|-------------|
| `get_score(wallet)` | `GET /v1/scores/{wallet}` | Latest scores for a wallet |
| `get_scores(asset_pair)` | `GET /v1/scores` | All scores, optionally filtered |
| `get_rings()` | `GET /v1/rings` | Detected wash-trading rings |
| `health()` | `GET /health` | API health check |

## Security

- **TLS verification is enabled by default.** Use
  `LedgerLensClient::danger_accept_invalid_certs()` only for local testing.
- **API keys are redacted from `Debug` output** to prevent accidental logging.
- **The ZK verifier is a pure verifier** — it never accepts secret blinding factors.

## Cross-Implementation Contract Test Vectors

The shared test fixture at `tests/fixtures/contract_vectors.json` (relative to the workspace root) contains canonical serialized instances of `RiskScore`, `Trade`, and `Asset`. It is generated from the canonical Python models via `python scripts/generate_contract_vectors.py` and consumed by **both the Python and Rust test suites**:

| Language | Test file | CI job |
|---|---|---|
| Python | `tests/test_contract_vectors.py` | `contract-vectors` |
| Rust | `crates/ledgerlens-sdk/tests/contract_vectors_test.rs` | `contract-vectors-rust` |
| TypeScript | `sdk/tests/contract_vectors.test.ts` | `contract-vectors-typescript` |

A change to any shared field that is not reflected in this fixture will fail CI in all three places. The test suite also includes adversarial vectors (wrong field name, out-of-range value) that prove divergence detection works, not just that the fixture can be parsed.

**To run the Rust contract vector tests:**

```bash
cargo test -p ledgerlens-sdk contract_vectors
```

See [ADR-005](../../docs/adr/ADR-005-schema-contract-enforcement.md) for the full design rationale.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a version history.

## Minimum Supported Rust Version (MSRV)

**No MSRV is currently pinned.** The CI `rust-sdk` job installs the latest
`stable` toolchain (via `actions-rs/toolchain@v1` with `toolchain: stable`)
without locking to a specific version, and `Cargo.toml` has no
`rust-version` field.

In practice the crate has an _implicit_ floor imposed by its dependencies:

| Constraint | Minimum Rust |
|------------|--------------|
| `edition = "2021"` | 1.56 |
| `reqwest 0.12` | 1.63 |

The effective floor is therefore **Rust ≥ 1.63**, but this is not tested or
enforced in CI. Older toolchains may fail to compile.

If you need a stable MSRV guarantee, open an issue to request that a
`rust-version` field be added to `Cargo.toml` and that CI be updated to
test against it.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a version history.

## License

MIT