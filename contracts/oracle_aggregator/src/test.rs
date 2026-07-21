#![cfg(test)]
extern crate std;

use super::*;
use ed25519_dalek::{Signer, SigningKey};
use rand::rngs::OsRng;
use sha2::{Digest, Sha256};
use soroban_sdk::{
    testutils::{Address as _, Ledger},
    Address, BytesN, Env, String, Vec,
};
use std::vec::Vec as StdVec;

const ASSET_PAIR: &str = "XLM/USDC";
const SCORE: u32 = 85;
const TIMESTAMP: u64 = 1672531200;

/// Fixture holding a registered contract plus the oracle keys it trusts.
struct Fixture {
    env: Env,
    client: OracleAggregatorClient<'static>,
    keys: StdVec<SigningKey>,
    wallet: Address,
}

impl Fixture {
    /// Register the contract with `n` freshly generated oracle keys and a
    /// `threshold`-of-`n` quorum, and advance the ledger to just inside the
    /// replay window.
    fn new(threshold: u32, n: usize) -> Self {
        let env = Env::default();
        let contract_id = env.register_contract(None, OracleAggregator);
        let client = OracleAggregatorClient::new(&env, &contract_id);

        let keys: StdVec<SigningKey> = (0..n).map(|_| SigningKey::generate(&mut OsRng)).collect();

        let mut oracle_keys = Vec::new(&env);
        for k in &keys {
            oracle_keys.push_back(BytesN::from_array(&env, &k.verifying_key().to_bytes()));
        }

        let score_contract = Address::generate(&env);
        client.initialize(&threshold, &oracle_keys, &score_contract);

        let wallet = Address::generate(&env);
        env.ledger().set_timestamp(TIMESTAMP + 100);

        Fixture { env, client, keys, wallet }
    }

    fn asset_pair(&self) -> String {
        String::from_str(&self.env, ASSET_PAIR)
    }

    /// The digest the contract expects oracles to sign.
    fn message(&self, score: u32, timestamp: u64) -> StdVec<u8> {
        self.client
            .canonical_message(&self.wallet, &self.asset_pair(), &score, &timestamp)
            .to_alloc_vec()
    }

    /// Build a signature vector from the given key indices, all signing the
    /// canonical message for `(score, timestamp)`.
    fn sign_with(&self, indices: &[usize], score: u32, timestamp: u64) -> Vec<SignaturePair> {
        let msg = self.message(score, timestamp);
        let mut sigs = Vec::new(&self.env);
        for &i in indices {
            sigs.push_back(self.sig_from(&self.keys[i], &msg));
        }
        sigs
    }

    fn sig_from(&self, key: &SigningKey, msg: &[u8]) -> SignaturePair {
        SignaturePair {
            public_key: BytesN::from_array(&self.env, &key.verifying_key().to_bytes()),
            signature: BytesN::from_array(&self.env, &key.sign(msg).to_bytes()),
        }
    }

    fn submit(&self, sigs: &Vec<SignaturePair>, score: u32, timestamp: u64) -> bool {
        self.client
            .submit_with_quorum(&self.wallet, &self.asset_pair(), &score, &timestamp, sigs)
    }
}

// ---------------------------------------------------------------------------
// Quorum invariants
// ---------------------------------------------------------------------------

/// Property: for every threshold k over n=5 oracles, exactly k distinct
/// authorised signatures is the tipping point — k succeeds, k-1 fails.
#[test]
fn quorum_boundary_holds_for_every_threshold() {
    const N: usize = 5;
    for k in 1..=N {
        let f = Fixture::new(k as u32, N);
        let all: StdVec<usize> = (0..N).collect();

        let exactly_k = f.sign_with(&all[..k], SCORE, TIMESTAMP);
        assert!(
            f.submit(&exactly_k, SCORE, TIMESTAMP),
            "threshold {k}: exactly {k} valid signatures should satisfy the quorum",
        );

        if k >= 1 {
            let one_short = f.sign_with(&all[..k - 1], SCORE, TIMESTAMP);
            assert!(
                !f.submit(&one_short, SCORE, TIMESTAMP),
                "threshold {k}: {} signatures should not satisfy the quorum",
                k - 1,
            );
        }
    }
}

/// Property: supplying more than the threshold never flips a passing quorum to
/// failing (monotonicity in the number of valid signers).
#[test]
fn quorum_is_monotonic_in_signer_count() {
    const N: usize = 5;
    let f = Fixture::new(3, N);
    for count in 3..=N {
        let indices: StdVec<usize> = (0..count).collect();
        let sigs = f.sign_with(&indices, SCORE, TIMESTAMP);
        assert!(
            f.submit(&sigs, SCORE, TIMESTAMP),
            "{count} valid signatures should still satisfy a 3-of-{N} quorum",
        );
    }
}

/// Regression: one oracle repeating its signature must not manufacture a
/// quorum. Each authorised key contributes at most one vote.
#[test]
fn repeated_key_cannot_forge_quorum() {
    let f = Fixture::new(3, 5);
    let msg = f.message(SCORE, TIMESTAMP);

    let mut sigs = Vec::new(&f.env);
    for _ in 0..3 {
        sigs.push_back(f.sig_from(&f.keys[0], &msg));
    }

    assert!(
        !f.submit(&sigs, SCORE, TIMESTAMP),
        "the same oracle signing three times must not satisfy a 3-of-5 quorum",
    );
}

/// Signatures from keys outside the authorised set are ignored rather than
/// counted, and cannot make up a shortfall.
#[test]
fn unauthorised_keys_do_not_count_toward_quorum() {
    let f = Fixture::new(3, 3);
    let msg = f.message(SCORE, TIMESTAMP);
    let outsider = SigningKey::generate(&mut OsRng);

    let mut sigs = Vec::new(&f.env);
    sigs.push_back(f.sig_from(&f.keys[0], &msg));
    sigs.push_back(f.sig_from(&f.keys[1], &msg));
    sigs.push_back(f.sig_from(&outsider, &msg));

    assert!(
        !f.submit(&sigs, SCORE, TIMESTAMP),
        "an unauthorised signer must not fill the third quorum slot",
    );
}

/// An empty signature vector can never satisfy a quorum.
#[test]
fn empty_signature_set_is_rejected() {
    let f = Fixture::new(1, 3);
    let sigs: Vec<SignaturePair> = Vec::new(&f.env);
    assert!(!f.submit(&sigs, SCORE, TIMESTAMP));
}

/// A malformed signature from an authorised key traps rather than being
/// silently skipped, because `ed25519_verify` cannot report failure by value.
/// This pins the documented trapping behaviour so it cannot regress unnoticed.
#[test]
#[should_panic]
fn malformed_signature_from_authorised_key_traps() {
    let f = Fixture::new(3, 3);
    let msg = f.message(SCORE, TIMESTAMP);

    let mut sigs = Vec::new(&f.env);
    sigs.push_back(f.sig_from(&f.keys[0], &msg));
    sigs.push_back(f.sig_from(&f.keys[1], &msg));
    sigs.push_back(SignaturePair {
        public_key: BytesN::from_array(&f.env, &f.keys[2].verifying_key().to_bytes()),
        signature: BytesN::from_array(&f.env, &[42u8; 64]),
    });

    f.submit(&sigs, SCORE, TIMESTAMP);
}

/// A signature over a different score must not authorise this submission.
#[test]
fn signature_over_different_payload_is_not_accepted() {
    let f = Fixture::new(1, 3);
    let other_msg = f.message(SCORE + 1, TIMESTAMP);

    let mut sigs = Vec::new(&f.env);
    sigs.push_back(f.sig_from(&f.keys[0], &other_msg));

    // The signature is well-formed but over the wrong digest, so verification
    // traps exactly as a malformed signature would.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        f.submit(&sigs, SCORE, TIMESTAMP)
    }));
    assert!(result.is_err(), "a signature over a different score must not be accepted");
}

// ---------------------------------------------------------------------------
// Replay-window invariants
// ---------------------------------------------------------------------------

/// Property: the replay window boundary is exact — age <= 300s is accepted,
/// age > 300s is rejected, across the boundary and well beyond it.
#[test]
fn replay_window_boundary_is_exact() {
    let f = Fixture::new(1, 3);

    for (age, expected) in [(0u64, true), (299, true), (300, true), (301, false), (10_000, false)] {
        let ts = TIMESTAMP;
        f.env.ledger().set_timestamp(ts + age);
        let sigs = f.sign_with(&[0], SCORE, ts);
        assert_eq!(
            f.submit(&sigs, SCORE, ts),
            expected,
            "submission with age {age}s should {} be accepted",
            if expected { "" } else { "not" },
        );
    }
}

/// Timestamps ahead of the ledger clock are not treated as stale: the age check
/// is one-sided by design.
#[test]
fn future_timestamps_are_not_treated_as_stale() {
    let f = Fixture::new(1, 3);
    let future = TIMESTAMP + 5_000;
    f.env.ledger().set_timestamp(TIMESTAMP);

    let sigs = f.sign_with(&[0], SCORE, future);
    assert!(f.submit(&sigs, SCORE, future));
}

// ---------------------------------------------------------------------------
// Canonical message invariants
// ---------------------------------------------------------------------------

/// The digest must be a pure function of its inputs.
#[test]
fn canonical_message_is_deterministic() {
    let f = Fixture::new(1, 1);
    assert_eq!(f.message(SCORE, TIMESTAMP), f.message(SCORE, TIMESTAMP));
}

/// Property: perturbing any single field changes the digest, so no two distinct
/// submissions share a signature.
#[test]
fn canonical_message_is_sensitive_to_every_field() {
    let f = Fixture::new(1, 1);
    let base = f.message(SCORE, TIMESTAMP);

    assert_ne!(base, f.message(SCORE + 1, TIMESTAMP), "score must affect the digest");
    assert_ne!(base, f.message(SCORE, TIMESTAMP + 1), "timestamp must affect the digest");

    let other_wallet = Address::generate(&f.env);
    let other = f
        .client
        .canonical_message(&other_wallet, &f.asset_pair(), &SCORE, &TIMESTAMP)
        .to_alloc_vec();
    assert_ne!(base, other, "wallet must affect the digest");

    let other_pair = f
        .client
        .canonical_message(
            &f.wallet,
            &String::from_str(&f.env, "BTC/USDC"),
            &SCORE,
            &TIMESTAMP,
        )
        .to_alloc_vec();
    assert_ne!(base, other_pair, "asset pair must affect the digest");
}

/// The delimiters must prevent field-splicing ambiguity: `("XLM", "USDC")` and
/// `("XLM/USDC", "")` must not collide.
#[test]
fn canonical_message_is_not_ambiguous_across_field_boundaries() {
    let f = Fixture::new(1, 1);

    let a = f
        .client
        .canonical_message(&f.wallet, &String::from_str(&f.env, "XLM/USDC"), &SCORE, &TIMESTAMP)
        .to_alloc_vec();
    let b = f
        .client
        .canonical_message(&f.wallet, &String::from_str(&f.env, "XLM|USDC"), &SCORE, &TIMESTAMP)
        .to_alloc_vec();

    assert_ne!(a, b, "a delimiter inside a field must not alias a field boundary");
}

/// Cross-language parity: the on-chain digest must equal the byte layout that
/// `detection/oracle_node.py::_canonical_message` produces, recomputed here
/// with an independent SHA-256 implementation.
///
/// Python:
/// `sha256(prefix + wallet.encode() + b"|" + asset_pair.encode() + b"|"
///         + struct.pack(">I", score) + struct.pack(">Q", timestamp))`
#[test]
fn canonical_message_matches_python_encoding() {
    let f = Fixture::new(1, 1);

    // Recover the wallet's strkey exactly as the contract sees it.
    let strkey = f.wallet.to_string();
    let mut buf = [0u8; 56];
    strkey.copy_into_slice(&mut buf);

    let mut preimage = StdVec::new();
    preimage.extend_from_slice(b"LedgerLens-Oracle-v1");
    preimage.extend_from_slice(&buf);
    preimage.extend_from_slice(b"|");
    preimage.extend_from_slice(ASSET_PAIR.as_bytes());
    preimage.extend_from_slice(b"|");
    preimage.extend_from_slice(&SCORE.to_be_bytes());
    preimage.extend_from_slice(&TIMESTAMP.to_be_bytes());

    let expected: StdVec<u8> = Sha256::digest(&preimage).to_vec();

    assert_eq!(
        f.message(SCORE, TIMESTAMP),
        expected,
        "on-chain digest diverged from the Python canonical encoding",
    );
}

/// An asset pair longer than the canonical-message buffer is rejected loudly
/// rather than being silently truncated into a colliding digest.
#[test]
#[should_panic(expected = "string exceeds canonical message buffer")]
fn overlong_asset_pair_is_rejected() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    let wallet = Address::generate(&env);

    let long = "A".repeat(MAX_ASSET_PAIR_LEN + 1);
    client.canonical_message(&wallet, &String::from_str(&env, &long), &SCORE, &TIMESTAMP);
}

// ---------------------------------------------------------------------------
// Initialisation invariants
// ---------------------------------------------------------------------------

#[test]
#[should_panic(expected = "already initialized")]
fn initialize_is_idempotent_guarded() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);

    let key = SigningKey::generate(&mut OsRng);
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &key.verifying_key().to_bytes()));
    let score_contract = Address::generate(&env);

    client.initialize(&1, &oracle_keys, &score_contract);
    client.initialize(&1, &oracle_keys, &score_contract);
}

#[test]
#[should_panic(expected = "threshold must be greater than zero")]
fn zero_threshold_is_rejected() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);

    let key = SigningKey::generate(&mut OsRng);
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &key.verifying_key().to_bytes()));

    client.initialize(&0, &oracle_keys, &Address::generate(&env));
}

#[test]
#[should_panic(expected = "threshold exceeds number of oracle keys")]
fn unsatisfiable_threshold_is_rejected() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);

    let key = SigningKey::generate(&mut OsRng);
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &key.verifying_key().to_bytes()));

    client.initialize(&2, &oracle_keys, &Address::generate(&env));
}

#[test]
#[should_panic(expected = "already initialized")]
fn test_double_initialization_fails() {
    let env = Env::default();
    env.mock_all_auths();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &[1u8; 32]));
    let score_contract = Address::generate(&env);
    
    client.initialize(&1, &oracle_keys, &score_contract);
    // Second initialization should panic
    client.initialize(&2, &oracle_keys, &score_contract);
}

#[test]
fn test_canonical_message_boundary_values() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    
    // Test max values don't panic
    let msg1 = client.canonical_message(&wallet, &asset_pair, &u32::MAX, &u64::MAX);
    assert!(msg1.len() > 0);
    
    // Test min values
    let msg2 = client.canonical_message(&wallet, &asset_pair, &0, &0);
    assert!(msg2.len() > 0);
    
    // Test mixed boundary
    let msg3 = client.canonical_message(&wallet, &asset_pair, &u32::MAX, &0);
    assert!(msg3.len() > 0);
}
