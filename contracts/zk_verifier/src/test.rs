#![cfg(test)]

use super::*;
use soroban_sdk::{Env, Bytes, BytesN, Address};
use crate::curve::Fq;

fn create_test_proof(
    a_x: &Fq,
    a_y: &Fq,
    b_x_0: &Fq,
    b_x_1: &Fq,
    b_y_0: &Fq,
    b_y_1: &Fq,
    c_x: &Fq,
    c_y: &Fq,
) -> Bytes {
    let env = Env::default();
    let mut proof = Bytes::new(&env);

    let to_bytes = |fq: &Fq| -> [u8; 32] {
        let mut arr = [0u8; 32];
        arr[0..16].copy_from_slice(&fq.0.to_le_bytes());
        arr[16..32].copy_from_slice(&fq.1.to_le_bytes());
        arr
    };

    proof.append(&Bytes::from_slice(&env, &to_bytes(a_x)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(a_y)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(b_x_0)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(b_x_1)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(b_y_0)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(b_y_1)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(c_x)));
    proof.append(&Bytes::from_slice(&env, &to_bytes(c_y)));

    proof
}

#[test]
fn test_verify_snark_below_threshold_success() {
    let env = Env::default();
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);

    let wallet = Address::generate(&env);
    let admin = Address::generate(&env);

    let px = Fq(12345, 0);
    let py = Fq(67890, 0);
    let threshold = 70;

    let mut px_arr = [0u8; 32];
    px_arr[0..16].copy_from_slice(&px.0.to_le_bytes());
    let mut py_arr = [0u8; 32];
    py_arr[0..16].copy_from_slice(&py.0.to_le_bytes());

    env.mock_all_auths();
    client.submit_score(
        &admin,
        &wallet,
        &85,
        &BytesN::from_array(&env, &[0u8; 32]),
        &BytesN::from_array(&env, &px_arr),
        &BytesN::from_array(&env, &py_arr),
    );

    // Create a structurally valid proof where:
    // a_x/a_y match px/py, c_x matches threshold, and other elements are valid
    let proof = create_test_proof(
        &px,
        &py,
        &Fq(42, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(threshold as u128, 0),
        &Fq(0, 0),
    );

    assert!(client.verify_snark_below_threshold(&wallet, &threshold, &proof));
}

#[test]
fn test_verify_snark_below_threshold_corrupted_fails() {
    let env = Env::default();
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);

    let wallet = Address::generate(&env);
    let admin = Address::generate(&env);

    let px = Fq(12345, 0);
    let py = Fq(67890, 0);
    let threshold = 70;

    let mut px_arr = [0u8; 32];
    px_arr[0..16].copy_from_slice(&px.0.to_le_bytes());
    let mut py_arr = [0u8; 32];
    py_arr[0..16].copy_from_slice(&py.0.to_le_bytes());

    env.mock_all_auths();
    client.submit_score(
        &admin,
        &wallet,
        &85,
        &BytesN::from_array(&env, &[0u8; 32]),
        &BytesN::from_array(&env, &px_arr),
        &BytesN::from_array(&env, &py_arr),
    );

    // Tampered: a_x does not match px
    let proof = create_test_proof(
        &Fq(9999, 0),
        &py,
        &Fq(42, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(threshold as u128, 0),
        &Fq(0, 0),
    );

    assert!(!client.verify_snark_below_threshold(&wallet, &threshold, &proof));
}

#[test]
fn test_verify_snark_below_threshold_invalid_bounds_fails() {
    let env = Env::default();
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);

    let wallet = Address::generate(&env);
    let admin = Address::generate(&env);

    let px = Fq(12345, 0);
    let py = Fq(67890, 0);
    let threshold = 70;

    let mut px_arr = [0u8; 32];
    px_arr[0..16].copy_from_slice(&px.0.to_le_bytes());
    let mut py_arr = [0u8; 32];
    py_arr[0..16].copy_from_slice(&py.0.to_le_bytes());

    env.mock_all_auths();
    client.submit_score(
        &admin,
        &wallet,
        &85,
        &BytesN::from_array(&env, &[0u8; 32]),
        &BytesN::from_array(&env, &px_arr),
        &BytesN::from_array(&env, &py_arr),
    );

    // Invalid bounds: a_x coordinate is larger than FIELD_MODULUS
    // FIELD_MODULUS_HI = 64352033668853702584149021272023910493
    let invalid_fq = Fq(0, 99999999999999999999999999999999999999);

    let proof = create_test_proof(
        &invalid_fq,
        &py,
        &Fq(42, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(0, 0),
        &Fq(threshold as u128, 0),
        &Fq(0, 0),
    );

    assert!(!client.verify_snark_below_threshold(&wallet, &threshold, &proof));
}
