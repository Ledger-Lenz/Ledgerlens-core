#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, BytesN, Vec, testutils::Address as _};
use oracle_aggregator::{OracleAggregator, OracleAggregatorClient, SignaturePair};

/// Authorization bypass fuzzing - ensure initialize cannot be called multiple times
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    threshold1: u32,
    threshold2: u32,
    key_count: u8,
}

fuzz_target!(|input: FuzzInput| {
    let key_count = ((input.key_count % 10) + 1) as usize;
    
    let env = Env::default();
    env.mock_all_auths();
    
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    // Generate oracle keys
    let mut oracle_keys = Vec::new(&env);
    for i in 0..key_count {
        let mut key = [0u8; 32];
        key[0] = i as u8;
        oracle_keys.push_back(BytesN::from_array(&env, &key));
    }
    
    let score_contract = Address::generate(&env);
    
    // First initialization should succeed (or fail with arithmetic issues)
    let first_init = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.initialize(&input.threshold1, &oracle_keys, &score_contract);
    }));
    
    // If first init failed, check it's not an auth bypass
    if first_init.is_err() {
        // Should only fail on intentional panics (not auth issues since we mock_all_auths)
        return;
    }
    
    // Second initialization MUST fail with "already initialized"
    let second_init = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.initialize(&input.threshold2, &oracle_keys, &score_contract);
    }));
    
    match second_init {
        Ok(_) => {
            panic!("Authorization bypass: initialize succeeded twice!");
        }
        Err(e) => {
            let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
                s.to_string()
            } else if let Some(s) = e.downcast_ref::<String>() {
                s.clone()
            } else {
                "unknown".to_string()
            };
            
            // Must panic with "already initialized" message
            assert!(
                panic_msg.contains("already initialized"),
                "Second initialize panicked with unexpected message: {}",
                panic_msg
            );
        }
    }
});
