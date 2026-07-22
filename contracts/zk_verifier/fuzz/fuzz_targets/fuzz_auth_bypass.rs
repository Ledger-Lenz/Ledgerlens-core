#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{Env, Address, BytesN, testutils::Address as _};
use ledgerlens_zk_verifier::{ZkVerifier, ZkVerifierClient};

/// Authorization bypass fuzzing - ensure submit_score requires admin auth
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    score: u32,
    commitment_seed: u64,
}

fuzz_target!(|input: FuzzInput| {
    let env = Env::default();
    // DELIBERATELY do NOT call env.mock_all_auths()
    // We want to test that authorization is actually required
    
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);
    
    let admin = Address::generate(&env);
    let wallet = Address::generate(&env);
    
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
    
    let pedersen_x = BytesN::from_array(&env, &[1u8; 32]);
    let pedersen_y = BytesN::from_array(&env, &[2u8; 32]);
    
    // Attempt to submit without authorization. cargo-fuzz always builds with
    // panic=abort (required for libFuzzer), so std::panic::catch_unwind can
    // never actually catch a panic here -- any panic from require_auth()
    // failing would abort the whole process and libFuzzer would misreport
    // the *correct* rejection as a crash. Use the non-panicking `try_`
    // client variant (every #[contractimpl] method gets one, returning
    // Result instead of panicking) so a failed auth check surfaces as a
    // plain Err instead of an abort.
    let result = client.try_submit_score(
        &admin,
        &wallet,
        &input.score,
        &commitment_hash,
        &pedersen_x,
        &pedersen_y,
    );

    // Expected: Err, because require_auth() rejects the call (no
    // mock_all_auths() and no real signature was provided). Ok would mean
    // submit_score stored a score with no authorization at all -- an
    // authorization bypass.
    if let Ok(inner) = result {
        let stored_score = client.get_score(&wallet);
        assert_eq!(
            stored_score, 0,
            "Authorization bypass detected: submit_score succeeded without auth (inner={:?}, stored score={})",
            inner, stored_score
        );
        panic!("Authorization bypass detected: submit_score returned Ok without any auth");
    }
});
