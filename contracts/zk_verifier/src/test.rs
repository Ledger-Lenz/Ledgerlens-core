#![cfg(test)]
extern crate std;

use std::string::String as StdString;
use std::vec::Vec as StdVec;

use soroban_sdk::{testutils::Address as _, Address, BytesN, Env, String as SorobanString};

use crate::curve::{add_mod, mul_mod, pow_mod, sub_mod, Fq, Fr, Point, U256, CURVE_ORDER, FIELD_MODULUS};
use crate::{ZkVerifier, ZkVerifierClient};

// ---------------------------------------------------------------------------
// Vector-file parsing helpers
// ---------------------------------------------------------------------------

fn hex32(s: &str) -> [u8; 32] {
    assert_eq!(s.len(), 64, "expected 64 hex chars, got {}: {}", s.len(), s);
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap();
    }
    out
}

fn hex_bytes(s: &str) -> StdVec<u8> {
    assert_eq!(s.len() % 2, 0);
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

// ---------------------------------------------------------------------------
// Modular arithmetic: 1,000+ vectors cross-checked against Python's
// arbitrary-precision (a * b) % m, (a + b) % m, (a - b) % m. Covers both
// FIELD_MODULUS (Fq's modulus) and CURVE_ORDER (Fr's modulus). Regenerate
// mod_arith_vectors.txt via the script in the PR description if the
// modulus constants ever change.
// ---------------------------------------------------------------------------

#[test]
fn mul_add_sub_mod_match_python_reference_vectors() {
    let data = include_str!("mod_arith_vectors.txt");
    let mut checked = 0usize;
    for line in data.lines() {
        let parts: StdVec<&str> = line.split_whitespace().collect();
        assert_eq!(parts.len(), 6, "malformed vector line: {}", line);
        let m = match parts[0] {
            "p" => FIELD_MODULUS,
            "n" => CURVE_ORDER,
            other => panic!("unknown modulus label {}", other),
        };
        let a = U256::from_be_bytes(&hex32(parts[1]));
        let b = U256::from_be_bytes(&hex32(parts[2]));
        let expected_mul = U256::from_be_bytes(&hex32(parts[3]));
        let expected_add = U256::from_be_bytes(&hex32(parts[4]));
        let expected_sub = U256::from_be_bytes(&hex32(parts[5]));

        assert_eq!(mul_mod(a, b, m), expected_mul, "mul_mod mismatch for {}", line);
        assert_eq!(add_mod(a, b, m), expected_add, "add_mod mismatch for {}", line);
        assert_eq!(sub_mod(a, b, m), expected_sub, "sub_mod mismatch for {}", line);
        checked += 1;
    }
    // The acceptance criterion asks for >= 1,000 randomized triples; the
    // fixture also includes deterministic edge cases (0, m-1, etc.) on top.
    assert!(checked >= 1000, "expected at least 1000 vectors, got {}", checked);
}

#[test]
fn mul_mod_handles_the_largest_possible_product() {
    // (p-1) * (p-1) exercises the full ~508-bit intermediate product --
    // the largest magnitude reduce_wide ever has to handle for Fq. Since
    // (p-1) == -1 mod p, (p-1)^2 mod p == 1.
    let p_minus_1 = sub_mod(FIELD_MODULUS, U256::ONE, FIELD_MODULUS);
    assert_eq!(mul_mod(p_minus_1, p_minus_1, FIELD_MODULUS), U256::ONE);

    let n_minus_1 = sub_mod(CURVE_ORDER, U256::ONE, CURVE_ORDER);
    assert_eq!(mul_mod(n_minus_1, n_minus_1, CURVE_ORDER), U256::ONE);
}

#[test]
fn pow_mod_fermat_inverse_round_trips() {
    // a * a^(m-2) mod m == 1 for a nonzero field element -- exercises the
    // exact exponentiation Fq::invert/Fr inversion is built on.
    let a = U256::from_u64(0x2a37) % FIELD_MODULUS;
    let m_minus_2 = sub_mod(FIELD_MODULUS, U256::from_u64(2), FIELD_MODULUS);
    let inv = pow_mod(a, m_minus_2, FIELD_MODULUS);
    assert_eq!(mul_mod(a, inv, FIELD_MODULUS), U256::ONE);
}

#[test]
fn fq_invert_matches_pow_mod() {
    let a = Fq::from_u64(123456789);
    let inv = a.invert();
    assert_eq!(a.mul(&inv), Fq::one());
}

// ---------------------------------------------------------------------------
// Point arithmetic sanity
// ---------------------------------------------------------------------------

#[test]
fn generator_is_on_curve() {
    let g = Point::generator();
    let (x, y) = g.to_affine();
    assert!(Point::from_affine_checked(x, y).is_some());
}

#[test]
fn h_generator_matches_python_reference() {
    // H = SHA256("LedgerLens ZK Generator H") mod n * G, computed
    // independently via py_ecc.bn128 (detection/zk_commitment.py::h_generator):
    //   digest = hashlib.sha256(b"LedgerLens ZK Generator H").digest()
    //   H = multiply(G1, int.from_bytes(digest, "big") % curve_order)
    // Cross-checks that the runtime-computed H here is the *same* point
    // Python uses, not merely "some" on-curve point derived from a hash.
    let env = Env::default();
    let h = Point::h_generator(&env);
    let (x, y) = h.to_affine();
    let expected_x = Fq::from_be_bytes(&hex32(H_X_HEX));
    let expected_y = Fq::from_be_bytes(&hex32(H_Y_HEX));
    assert_eq!(x, expected_x, "H.x does not match the Python-computed reference value");
    assert_eq!(y, expected_y, "H.y does not match the Python-computed reference value");
    assert!(Point::from_affine_checked(x, y).is_some());
}

const H_X_HEX: &str = "1f16cc48d0aca8c7a8808a252d17f2f13aa684244278e06b4bcd4eefae61a8e2";
const H_Y_HEX: &str = "20cc20fd4c76b034ea2b126a39f61a4481e7634333d171a9ccb1ff943bfd1fb8";

#[test]
fn double_and_add_via_scalar_mult_agree() {
    let g = Point::generator();
    let two_g_via_double = g.double();
    let two_g_via_mul = g.mul_scalar(&Fr::from_u64(2));
    assert!(two_g_via_double.eq(&two_g_via_mul));

    let five_g = g.mul_scalar(&Fr::from_u64(5));
    let five_g_manual = g.add(&g).add(&g).add(&g).add(&g);
    assert!(five_g.eq(&five_g_manual));
}

#[test]
fn point_addition_with_identity_is_noop() {
    let g = Point::generator();
    let inf = Point::infinity();
    assert!(g.add(&inf).eq(&g));
    assert!(inf.add(&g).eq(&g));
}

#[test]
fn point_plus_its_negation_is_identity() {
    let g = Point::generator();
    let sum = g.add(&g.neg());
    assert!(sum.is_infinity());
}

#[test]
fn off_curve_point_is_rejected() {
    let bad = Point::from_affine_checked(Fq::from_u64(1), Fq::from_u64(1));
    assert!(bad.is_none(), "(1,1) is not on y^2 = x^3 + 3");
}

// ---------------------------------------------------------------------------
// Fiat-Shamir: changing any single input changes the output (no collision
// on the sampled input space -- acceptance criterion 6), and the function
// is a pure/deterministic function of its inputs.
// ---------------------------------------------------------------------------

#[test]
fn fiat_shamir_changes_with_any_single_input() {
    let env = Env::default();
    let g = Point::generator();
    let h = Point::h_generator(&env);
    let ctx_a: BytesN<32> = BytesN::from_array(&env, &[1u8; 32]);
    let ctx_b: BytesN<32> = BytesN::from_array(&env, &[2u8; 32]);

    let base = ZkVerifier::fiat_shamir(&env, &g, &h, &g, &ctx_a);
    let vary_r0 = ZkVerifier::fiat_shamir(&env, &h, &h, &g, &ctx_a);
    let vary_r1 = ZkVerifier::fiat_shamir(&env, &g, &g, &g, &ctx_a);
    let vary_b = ZkVerifier::fiat_shamir(&env, &g, &h, &h, &ctx_a);
    let vary_ctx = ZkVerifier::fiat_shamir(&env, &g, &h, &g, &ctx_b);

    assert!(!base.eq(&vary_r0));
    assert!(!base.eq(&vary_r1));
    assert!(!base.eq(&vary_b));
    assert!(!base.eq(&vary_ctx));
}

#[test]
fn fiat_shamir_is_deterministic() {
    let env = Env::default();
    let g = Point::generator();
    let ctx: BytesN<32> = BytesN::from_array(&env, &[7u8; 32]);
    let a = ZkVerifier::fiat_shamir(&env, &g, &g, &g, &ctx);
    let b = ZkVerifier::fiat_shamir(&env, &g, &g, &g, &ctx);
    assert!(a.eq(&b));
}

// ---------------------------------------------------------------------------
// Cross-language integration: a real proof generated by
// detection/zk_prover.py::generate_threshold_proof, wire-encoded via
// serialize_proof_bytes, verified here through the actual Soroban contract
// invocation (not just the internal Rust functions). Fixture file
// (zk_test_vectors.txt) generation script is in the PR description --
// regenerate it if PROOF_WIRE_VERSION or the curve parameters change.
// ---------------------------------------------------------------------------

struct Fixture {
    wallet: StdString,
    other_wallet: StdString,
    threshold: u32,
    pedersen_x: [u8; 32],
    pedersen_y: [u8; 32],
    good: StdVec<u8>,
    tamper_bitflip_commit: StdVec<u8>,
    tamper_c0: StdVec<u8>,
    tamper_swap: StdVec<u8>,
    tamper_score_commit: StdVec<u8>,
    tamper_s1: StdVec<u8>,
    tamper_truncated: StdVec<u8>,
    tamper_bad_version: StdVec<u8>,
}

fn load_fixture() -> Fixture {
    let data = include_str!("zk_test_vectors.txt");
    let mut wallet = StdString::new();
    let mut other_wallet = StdString::new();
    let mut threshold = 0u32;
    let mut pedersen_x = [0u8; 32];
    let mut pedersen_y = [0u8; 32];
    let mut good = StdVec::new();
    let mut tamper_bitflip_commit = StdVec::new();
    let mut tamper_c0 = StdVec::new();
    let mut tamper_swap = StdVec::new();
    let mut tamper_score_commit = StdVec::new();
    let mut tamper_s1 = StdVec::new();
    let mut tamper_truncated = StdVec::new();
    let mut tamper_bad_version = StdVec::new();

    for line in data.lines() {
        let mut parts = line.splitn(2, ' ');
        let key = parts.next().unwrap();
        let value = parts.next().unwrap();
        match key {
            "WALLET" => wallet = StdString::from(value),
            "OTHER_WALLET" => other_wallet = StdString::from(value),
            "THRESHOLD" => threshold = value.parse().unwrap(),
            "PEDERSEN_X" => pedersen_x = hex32(value),
            "PEDERSEN_Y" => pedersen_y = hex32(value),
            "GOOD" => good = hex_bytes(value),
            "TAMPER_BITFLIP_COMMIT" => tamper_bitflip_commit = hex_bytes(value),
            "TAMPER_C0" => tamper_c0 = hex_bytes(value),
            "TAMPER_SWAP" => tamper_swap = hex_bytes(value),
            "TAMPER_SCORE_COMMIT" => tamper_score_commit = hex_bytes(value),
            "TAMPER_S1" => tamper_s1 = hex_bytes(value),
            "TAMPER_TRUNCATED" => tamper_truncated = hex_bytes(value),
            "TAMPER_BAD_VERSION" => tamper_bad_version = hex_bytes(value),
            other => panic!("unknown fixture key {}", other),
        }
    }
    Fixture {
        wallet,
        other_wallet,
        threshold,
        pedersen_x,
        pedersen_y,
        good,
        tamper_bitflip_commit,
        tamper_c0,
        tamper_swap,
        tamper_score_commit,
        tamper_s1,
        tamper_truncated,
        tamper_bad_version,
    }
}

struct TestSetup {
    env: Env,
    client: ZkVerifierClient<'static>,
    wallet: Address,
    admin: Address,
}

fn setup(f: &Fixture) -> TestSetup {
    let env = Env::default();
    env.mock_all_auths();
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);

    let admin = Address::generate(&env);
    let wallet = Address::from_string(&SorobanString::from_str(&env, &f.wallet));

    client.submit_score(
        &admin,
        &wallet,
        &70, // legacy numeric score field; independent of the ZK path under test
        &BytesN::from_array(&env, &[0u8; 32]),
        &BytesN::from_array(&env, &f.pedersen_x),
        &BytesN::from_array(&env, &f.pedersen_y),
    );

    TestSetup { env, client, wallet, admin }
}

fn bytes_from(env: &Env, v: &[u8]) -> soroban_sdk::Bytes {
    soroban_sdk::Bytes::from_slice(env, v)
}

#[test]
fn honest_proof_from_python_prover_is_accepted() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.good);
    assert!(
        s.client.verify_threshold(&s.wallet, &f.threshold, &proof),
        "a proof honestly generated by detection/zk_prover.py must verify on-chain"
    );
}

#[test]
fn tampered_bit_commitment_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_bitflip_commit);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn tampered_challenge_share_c0_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_c0);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn swapped_bit_records_are_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_swap);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn tampered_score_commitment_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_score_commit);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn tampered_response_s1_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_s1);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn truncated_proof_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_truncated);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn wrong_wire_version_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.tamper_bad_version);
    assert!(!s.client.verify_threshold(&s.wallet, &f.threshold, &proof));
}

#[test]
fn replayed_for_a_different_wallet_is_rejected() {
    // A proof honestly generated for `wallet` must not verify against a
    // *different* wallet's on-chain record, even if that wallet happens to
    // have the exact same Pedersen commitment on file (proof_context's
    // transcript is wallet-bound, so the Fiat-Shamir challenge the proof
    // was built against no longer matches).
    let f = load_fixture();
    let s = setup(&f);
    let other_wallet = Address::from_string(&SorobanString::from_str(&s.env, &f.other_wallet));
    s.client.submit_score(
        &s.admin,
        &other_wallet,
        &70,
        &BytesN::from_array(&s.env, &[0u8; 32]),
        &BytesN::from_array(&s.env, &f.pedersen_x),
        &BytesN::from_array(&s.env, &f.pedersen_y),
    );
    let proof = bytes_from(&s.env, &f.good);
    assert!(!s.client.verify_threshold(&other_wallet, &f.threshold, &proof));
}

#[test]
fn wrong_threshold_is_rejected() {
    let f = load_fixture();
    let s = setup(&f);
    let proof = bytes_from(&s.env, &f.good);
    assert!(!s.client.verify_threshold(&s.wallet, &(f.threshold + 5), &proof));
}

#[test]
fn no_commitment_on_record_is_rejected() {
    let env = Env::default();
    env.mock_all_auths();
    let contract_id = env.register_contract(None, ZkVerifier);
    let client = ZkVerifierClient::new(&env, &contract_id);
    let f = load_fixture();
    let unknown_wallet = Address::from_string(&SorobanString::from_str(&env, &f.wallet));
    let proof = bytes_from(&env, &f.good);
    assert!(!client.verify_threshold(&unknown_wallet, &f.threshold, &proof));
}
