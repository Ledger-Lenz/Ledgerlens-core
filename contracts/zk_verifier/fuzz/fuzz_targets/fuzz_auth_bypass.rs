#![no_main]

use libfuzzer_sys::fuzz_target;
use arbitrary::Arbitrary;
use soroban_sdk::{
    testutils::{Address as _, MockAuth, MockAuthInvoke},
    Address, BytesN, Env, IntoVal,
};
use ledgerlens_zk_verifier::{ZkVerifier, ZkVerifierClient};

/// Authorization bypass fuzzing — submit_score must require the *stored*
/// admin identity, not merely that some address signed the call.
#[derive(Arbitrary, Debug)]
struct FuzzInput {
    score: u32,
    commitment_seed: u64,
}

fn commitment_from_seed(env: &Env, seed: u64) -> BytesN<32> {
    BytesN::from_array(
        env,
        &seed
            .to_le_bytes()
            .iter()
            .cycle()
            .take(32)
            .copied()
            .collect::<Vec<_>>()
            .try_into()
            .unwrap(),
    )
}

fn try_submit(
    client: &ZkVerifierClient<'_>,
    wallet: &Address,
    score: u32,
    commitment_hash: &BytesN<32>,
    pedersen_x: &BytesN<32>,
    pedersen_y: &BytesN<32>,
) {
    // cargo-fuzz always builds with panic=abort, so catch_unwind cannot
    // catch a require_auth() trap. The non-panicking `try_` client
    // variant surfaces rejection as Err instead of aborting the process.
    let result = client.try_submit_score(wallet, &score, commitment_hash, pedersen_x, pedersen_y);

    if let Ok(inner) = result {
        let stored_score = client.get_score(wallet);
        assert_eq!(
            stored_score, 0,
            "Authorization bypass detected: submit_score succeeded (inner={:?}, stored score={})",
            inner, stored_score
        );
        panic!("Authorization bypass detected: submit_score returned Ok");
    }
}

fuzz_target!(|input: FuzzInput| {
    let env = Env::default();
    // DELIBERATELY do NOT call env.mock_all_auths()

    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);

    let stored_admin = Address::generate(&env);
    let attacker = Address::generate(&env);
    let wallet = Address::generate(&env);

    client.initialize(&stored_admin);

    let commitment_hash = commitment_from_seed(&env, input.commitment_seed);
    let pedersen_x = BytesN::from_array(&env, &[1u8; 32]);
    let pedersen_y = BytesN::from_array(&env, &[2u8; 32]);

    // 1. No signatures at all — must fail.
    try_submit(
        &client,
        &wallet,
        input.score,
        &commitment_hash,
        &pedersen_x,
        &pedersen_y,
    );

    // 2. Self-signed non-admin — the actual bypass: attacker authenticates
    //    as themselves. The stored admin did not sign, so this must fail.
    env.mock_auths(&[MockAuth {
        address: &attacker,
        invoke: &MockAuthInvoke {
            contract: &contract_id,
            fn_name: "submit_score",
            args: (
                wallet.clone(),
                input.score,
                commitment_hash.clone(),
                pedersen_x.clone(),
                pedersen_y.clone(),
            )
                .into_val(&env),
            sub_invokes: &[],
        },
    }]);

    try_submit(
        &client,
        &wallet,
        input.score,
        &commitment_hash,
        &pedersen_x,
        &pedersen_y,
    );
});
