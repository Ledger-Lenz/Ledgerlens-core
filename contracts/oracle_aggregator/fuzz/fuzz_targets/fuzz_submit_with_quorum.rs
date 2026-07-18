#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, Symbol, BytesN, Vec, testutils::Address as _};
use oracle_aggregator::{OracleAggregator, OracleAggregatorClient, SignaturePair};

/// Bounded input structure to prevent unbounded memory allocation during fuzzing
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    threshold: u32,
    oracle_key_count: u8,  // bounded to prevent OOM
    signature_count: u8,   // bounded to prevent OOM
    score: u32,
    timestamp: u64,
    ledger_timestamp: u64,
    // Raw bytes for signatures and keys (we'll construct from these)
    key_seed: u64,
    sig_seed: u64,
}

fuzz_target!(|input: FuzzInput| {
    // Bound counts to reasonable fuzzing ranges
    let oracle_key_count = (input.oracle_key_count % 20).max(1); // 1-20 keys
    let signature_count = (input.signature_count % 25).max(0); // 0-25 signatures
    let threshold = input.threshold;
    
    let env = Env::default();
    env.mock_all_auths();
    
    // Register contract
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    // Generate oracle keys deterministically from seed
    let mut oracle_keys = Vec::new(&env);
    for i in 0..oracle_key_count {
        let key_bytes = (input.key_seed.wrapping_add(i as u64)).to_le_bytes();
        let mut key = [0u8; 32];
        for j in 0..4 {
            key[j * 8..(j + 1) * 8].copy_from_slice(&key_bytes);
        }
        oracle_keys.push_back(BytesN::from_array(&env, &key));
    }
    
    let score_contract = Address::generate(&env);
    
    // Attempt to initialize - this may panic if threshold is invalid (intentional)
    let init_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.initialize(&threshold, &oracle_keys, &score_contract);
    }));
    
    // Only intentional "already initialized" panics are acceptable
    if let Err(e) = init_result {
        let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
            s.to_string()
        } else if let Some(s) = e.downcast_ref::<String>() {
            s.clone()
        } else {
            "unknown panic".to_string()
        };
        
        // Allow only known intentional panics
        assert!(
            panic_msg.contains("already initialized"),
            "Unexpected panic during initialize: {}",
            panic_msg
        );
        return; // Skip rest of test if initialization panicked intentionally
    }
    
    // Generate signatures
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    
    let mut signatures = Vec::new(&env);
    for i in 0..signature_count {
        let key_idx = (input.sig_seed.wrapping_add(i as u64)) % (oracle_key_count as u64);
        let key_bytes = (input.key_seed.wrapping_add(key_idx)).to_le_bytes();
        let mut key = [0u8; 32];
        for j in 0..4 {
            key[j * 8..(j + 1) * 8].copy_from_slice(&key_bytes);
        }
        
        let sig_bytes = (input.sig_seed.wrapping_add(i as u64)).to_le_bytes();
        let mut sig = [0u8; 64];
        for j in 0..8 {
            sig[j * 8..(j + 1) * 8].copy_from_slice(&sig_bytes);
        }
        
        signatures.push_back(SignaturePair {
            public_key: BytesN::from_array(&env, &key),
            signature: BytesN::from_array(&env, &sig),
        });
    }
    
    // Set ledger timestamp
    env.ledger().set_timestamp(input.ledger_timestamp);
    
    // Call submit_with_quorum - should never panic unintentionally
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        client.submit_with_quorum(
            &wallet,
            &asset_pair,
            &input.score,
            &input.timestamp,
            &signatures,
        )
    }));
    
    // If panic occurs, it must be an allowed intentional panic
    if let Err(e) = result {
        let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
            s.to_string()
        } else if let Some(s) = e.downcast_ref::<String>() {
            s.clone()
        } else {
            "unknown panic".to_string()
        };
        
        // No intentional panics expected in submit_with_quorum
        // Any panic here is a bug (overflow, unwrap failure, etc.)
        panic!("Unexpected panic in submit_with_quorum: {}", panic_msg);
    }
});
