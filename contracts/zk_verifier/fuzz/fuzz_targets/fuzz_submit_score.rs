#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, BytesN, testutils::Address as _};
use ledgerlens_zk_verifier::{ZkVerifier, ZkVerifierClient};

/// Fuzz inputs for submit_score to test boundary conditions and overflow
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    score: u32,
    // Use seeds to generate commitment bytes deterministically
    commitment_seed: u64,
    pedersen_x_seed: u64,
    pedersen_y_seed: u64,
}

fuzz_target!(|input: FuzzInput| {
    let env = Env::default();
    env.mock_all_auths(); // Mock authorization for fuzzing
    
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);
    
    let admin = Address::generate(&env);
    let wallet = Address::generate(&env);
    
    // Generate commitment bytes from seeds
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
    
    // Test boundary values explicitly
    let test_scores = vec![
        input.score,
        0,          // min score
        100,        // typical max
        u32::MAX,   // absolute max
    ];
    
    for score in test_scores {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            client.submit_score(
                &admin,
                &wallet,
                &score,
                &commitment_hash,
                &pedersen_x,
                &pedersen_y,
            );
        }));
        
        if let Err(e) = result {
            let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
                s.to_string()
            } else if let Some(s) = e.downcast_ref::<String>() {
                s.clone()
            } else {
                "unknown panic".to_string()
            };
            
            // submit_score should never panic - it just stores data
            panic!(
                "Unexpected panic in submit_score with score={}: {}",
                score, panic_msg
            );
        }
    }
    
    // Verify we can read back the score without panic
    let get_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.get_score(&wallet)
    }));
    
    assert!(
        get_result.is_ok(),
        "get_score panicked after successful submit_score"
    );
});
