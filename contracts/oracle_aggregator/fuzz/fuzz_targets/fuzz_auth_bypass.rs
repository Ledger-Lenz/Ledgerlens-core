#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, BytesN, Vec, testutils::Address as _};
use oracle_aggregator::{OracleAggregator, OracleAggregatorClient};

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

    // `initialize` legitimately panics on invalid `threshold1` (zero, or
    // greater than key_count) -- an intentional rejection, not a bug.
    // cargo-fuzz builds always use panic=abort, so catch_unwind can never
    // catch that there either; use the non-panicking try_ variant.
    if client
        .try_initialize(&input.threshold1, &oracle_keys, &score_contract)
        .is_err()
    {
        return;
    }

    // Second initialization MUST fail (already initialized) -- this is the
    // actual property under test.
    let second_init = client.try_initialize(&input.threshold2, &oracle_keys, &score_contract);
    assert!(
        second_init.is_err(),
        "Authorization bypass: initialize succeeded twice!"
    );
});
