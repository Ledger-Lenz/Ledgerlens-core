#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, testutils::Address as _};
use oracle_aggregator::{OracleAggregator, OracleAggregatorClient};

/// Fuzz inputs for canonical_message covering boundary cases
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    score: u32,
    timestamp: u64,
    // Use bounded string length to prevent OOM
    asset_pair_len: u8,
    asset_pair_seed: u64,
}

fuzz_target!(|input: FuzzInput| {
    let env = Env::default();
    env.mock_all_auths();
    
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let wallet = Address::generate(&env);
    
    // Create asset pair string with bounded length (1-10 chars)
    let asset_len = ((input.asset_pair_len % 10) + 1) as usize;
    let mut asset_str = String::new();
    for i in 0..asset_len {
        let byte = ((input.asset_pair_seed.wrapping_add(i as u64) % 26) + 65) as u8; // A-Z
        asset_str.push(byte as char);
    }
    // canonical_message takes asset_pair: soroban_sdk::String, not Symbol --
    // fully-qualified here to avoid colliding with std::string::String, used
    // above to build asset_str char-by-char.
    let asset_pair = soroban_sdk::String::from_str(&env, &asset_str);
    
    // Test boundary values explicitly
    let test_cases = vec![
        (input.score, input.timestamp),
        (0, input.timestamp),              // min score
        (u32::MAX, input.timestamp),        // max score
        (input.score, 0),                   // min timestamp
        (input.score, u64::MAX),            // max timestamp
        (u32::MAX, u64::MAX),               // both max
        (0, 0),                             // both min
    ];
    
    for (score, timestamp) in test_cases {
        // canonical_message is pure byte packing and should never reject any
        // of these bounded inputs. Uses try_canonical_message, not
        // catch_unwind(canonical_message) -- cargo-fuzz builds always use
        // panic=abort, so catch_unwind could never catch a real regression
        // here either; it would just abort silently with a less useful
        // libFuzzer-reported crash instead of this assertion.
        let result = client.try_canonical_message(&wallet, &asset_pair, &score, &timestamp);
        assert!(
            result.is_ok(),
            "Unexpected rejection in canonical_message with score={}, timestamp={}",
            score, timestamp
        );
    }
});
