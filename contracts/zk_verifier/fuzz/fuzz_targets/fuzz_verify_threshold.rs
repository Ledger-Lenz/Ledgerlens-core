#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, Bytes, BytesN, testutils::Address as _};
use ledgerlens_zk_verifier::{ZkVerifier, ZkVerifierClient};

/// Fuzz inputs for verify_threshold with malformed proofs
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    threshold: u32,
    proof_len: u16,
    proof_seed: u64,
    // For setting up a score commitment
    score: u32,
    commitment_seed: u64,
    pedersen_x_seed: u64,
    pedersen_y_seed: u64,
}

fuzz_target!(|input: FuzzInput| {
    let env = Env::default();
    env.mock_all_auths();
    
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);
    
    let admin = Address::generate(&env);
    let wallet = Address::generate(&env);
    
    // Set up a score commitment first
    let commitment_hash = BytesN::from_array(
        &env,
        &input.commitment_seed.to_le_bytes()
            .iter()
            .cycle()
            .take(32)
            .copied()
            .collect::<Vec<_>>()
            .try_into()
            .unwrap()
    );
    
    let pedersen_x = BytesN::from_array(
        &env,
        &input.pedersen_x_seed.to_le_bytes()
            .iter()
            .cycle()
            .take(32)
            .copied()
            .collect::<Vec<_>>()
            .try_into()
            .unwrap()
    );
    
    let pedersen_y = BytesN::from_array(
        &env,
        &input.pedersen_y_seed.to_le_bytes()
            .iter()
            .cycle()
            .take(32)
            .copied()
            .collect::<Vec<_>>()
            .try_into()
            .unwrap()
    );
    
    client.submit_score(
        &admin,
        &wallet,
        &input.score,
        &commitment_hash,
        &pedersen_x,
        &pedersen_y,
    );
    
    // Generate malformed proof bytes
    let proof_len = (input.proof_len % 1024) as usize; // Bounded to prevent OOM
    let mut proof_bytes = vec![];
    for i in 0..proof_len {
        let byte = ((input.proof_seed.wrapping_add(i as u64)) % 256) as u8;
        proof_bytes.push(byte);
    }
    let proof = Bytes::from_slice(&env, &proof_bytes);
    
    // Test various threshold values including boundary cases
    let test_thresholds = vec![
        input.threshold,
        0,
        50,
        100,
        input.score,              // threshold = score
        input.score.saturating_add(1), // threshold = score + 1
        u32::MAX,
    ];
    
    for threshold in test_thresholds {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            client.verify_threshold(&wallet, &threshold, &proof)
        }));
        
        if let Err(e) = result {
            let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
                s.to_string()
            } else if let Some(s) = e.downcast_ref::<String>() {
                s.clone()
            } else {
                "unknown panic".to_string()
            };
            
            // verify_threshold should handle malformed proofs gracefully
            // It should return false, not panic
            panic!(
                "Unexpected panic in verify_threshold with threshold={}, proof_len={}: {}",
                threshold, proof_len, panic_msg
            );
        }
    }
    
    // Test with empty proof
    let empty_proof = Bytes::new(&env);
    let empty_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.verify_threshold(&wallet, &50, &empty_proof)
    }));
    
    assert!(
        empty_result.is_ok(),
        "verify_threshold panicked on empty proof"
    );
});
