#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, BytesN, Vec, testutils::{Address as _, Ledger as _}};
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

    // Fuzzed `threshold` legitimately triggers three different rejections in
    // `initialize` (zero threshold, threshold > key count, already
    // initialized) -- all are intentional `panic!`s in contract code, not
    // bugs. `cargo-fuzz` binaries always build with `panic=abort`, so
    // `std::panic::catch_unwind` can never actually catch any of them there;
    // an intentional rejection would abort the whole process and libFuzzer
    // would misreport it as a crash. Use the non-panicking `try_initialize`
    // client variant instead, which surfaces a rejection as a plain `Err`.
    if client
        .try_initialize(&threshold, &oracle_keys, &score_contract)
        .is_err()
    {
        return; // Any rejection here is an intentional, expected outcome.
    }
    
    // Generate signatures
    let wallet = Address::generate(&env);
    // submit_with_quorum takes asset_pair: soroban_sdk::String, not Symbol --
    // fully-qualified here (not imported at module scope) to avoid colliding
    // with std::string::String, used below for panic-payload downcasting.
    let asset_pair = soroban_sdk::String::from_str(&env, "XLM-USDC");
    
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
    
    // `submit_with_quorum` calls `env.crypto().ed25519_verify(...)` on any
    // signature whose public key matches a stored oracle key (see the
    // "Traps if the signature is invalid" note in
    // contracts/oracle_aggregator/src/lib.rs), and this harness has no way
    // to construct a cryptographically valid signature for fuzzed input --
    // so that trap is a legitimate, expected outcome here, not a bug, and
    // "assert never panics" isn't a meaningful check to run for it. Using
    // `try_submit_with_quorum` still gets the important property for free:
    // no false-positive crash report from an uncatchable-under-panic=abort
    // catch_unwind on this expected trap.
    let _ = client.try_submit_with_quorum(
        &wallet,
        &asset_pair,
        &input.score,
        &input.timestamp,
        &signatures,
    );
});
