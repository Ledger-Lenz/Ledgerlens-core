/// Example: Soroban contract integrator verifying a ZK threshold proof.
///
/// This example shows how an off-chain Rust service (e.g. an exchange or
/// custodian that already runs Soroban tooling) can verify a ThresholdProof
/// from the LedgerLens API without spinning up a Python interpreter.
///
/// Run with:
/// ```bash
/// cargo run --example soroban_integration --features zk-verify
/// ```
use ledgerlens_sdk::{verify_threshold_proof, ThresholdProof};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Simulated proof from the LedgerLens API response.
    // In production, this would be deserialized from the actual API response body.
    let proof = ThresholdProof {
        score_commit_x: "0".to_string(),
        score_commit_y: "0".to_string(),
        bits: vec![
            // 7 bit proofs (2^7 = 128 >= 100)
            // In a real proof, these would contain valid BN254 curve points
            // and Fiat-Shamir challenges.
            ledgerlens_sdk::BitProof {
                commit_x: "0".to_string(),
                commit_y: "0".to_string(),
                c0: "0".to_string(),
                c1: "0".to_string(),
                s0: "0".to_string(),
                s1: "0".to_string(),
            };
            7
        ],
    };

    // Verify that the committed score >= 50 for wallet "GA…"
    let wallet = "GA1234567890ABCDEF";
    let threshold = 50;

    match verify_threshold_proof(&proof, threshold, wallet) {
        Ok(true) => println!(
            "ZK proof VALID: wallet {} has score >= {}",
            wallet, threshold
        ),
        Ok(false) => println!(
            "ZK proof INVALID: wallet {} may have score < {}",
            wallet, threshold
        ),
        Err(e) => eprintln!("ZK proof verification error: {}", e),
    }

    Ok(())
}