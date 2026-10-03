//! Tests for the risk-score registry contract.
//!
//! # Core invariants
//!
//! The registry must uphold the following properties for *every* possible
//! sequence of calls, not merely for the hand-picked examples below:
//!
//! 1. **Monotonic versioning** — a score for a given subject is only ever
//!    replaced by a strictly greater version. Reads therefore never observe a
//!    version going backwards, and the stored version equals the number of
//!    successful writes for that subject.
//! 2. **Authorized-publisher-only writes** — only an address that has been
//!    registered as a publisher may mutate state. Any call from an
//!    unregistered address must fail and leave the registry untouched.
//! 3. **No partially-written reads** — a read either returns the last fully
//!    committed `(score, version)` pair or fails; it never returns a torn or
//!    half-applied value.
//!
//! The property-based tests below fuzz arbitrary call sequences (including
//! unauthorized callers) and assert these invariants after every step.

use super::*;
use soroban_sdk::testutils::Address as _;
use soroban_sdk::{Address, Env, Vec};

/// A single fuzzed operation against the registry.
#[derive(Clone)]
enum Op {
    /// Register `publisher` as an authorized writer.
    Register(Address),
    /// Attempt a write from `caller` for `subject` with `score`.
    Write(Address, Address, i128),
    /// Read the current score for `subject`.
    Read(Address),
}

/// Deterministic, dependency-free pseudo-random generator so the fuzzing is
/// reproducible across runs and CI environments.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed | 1)
    }

    fn next(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        if n == 0 {
            0
        } else {
            self.next() % n
        }
    }
}

/// Build a pool of distinct addresses used as publishers/subjects.
fn address_pool(env: &Env, n: u64) -> Vec<Address> {
    let mut pool = Vec::new(env);
    for _ in 0..n {
        pool.push_back(Address::generate(env));
    }
    pool
}

/// Generate a random call sequence of `len` operations.
fn gen_ops(env: &Env, rng: &mut Rng, pool: &Vec<Address>, len: u64) -> Vec<Op> {
    let mut ops = Vec::new(env);
    let n = pool.len() as u64;
    for _ in 0..len {
        let a = pool.get(rng.below(n) as u32).unwrap();
        let b = pool.get(rng.below(n) as u32).unwrap();
        match rng.below(3) {
            0 => ops.push_back(Op::Register(a)),
            1 => ops.push_back(Op::Write(a, b, rng.next() as i128)),
            _ => ops.push_back(Op::Read(b)),
        }
    }
    ops
}

/// Fuzz arbitrary call sequences and assert every core invariant holds after
/// each step. This is the property-based counterpart to the example tests.
#[test]
fn prop_invariants_hold_for_arbitrary_call_sequences() {
    for seed in 1..64u64 {
        let env = Env::default();
        env.mock_all_auths();
        let contract_id = env.register_contract(None, RiskScoreRegistry);
        let client = RiskScoreRegistryClient::new(&env, &contract_id);

        let mut rng = Rng::new(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15));
        let pool = address_pool(&env, 4);
        let ops = gen_ops(&env, &mut rng, &pool, 40);

        // Shadow model: expected version per subject, and the set of
        // publishers we have successfully registered.
        let mut expected_version: Vec<(Address, u32)> = Vec::new(&env);
        let mut authorized: Vec<Address> = Vec::new(&env);

        for op in ops.iter() {
            match op {
                Op::Register(publisher) => {
                    client.register_publisher(&publisher);
                    if !authorized.contains(&publisher) {
                        authorized.push_back(publisher.clone());
                    }
                }
                Op::Write(caller, subject, score) => {
                    let before = client.get_score(&subject);
                    let result = client.try_set_score(&caller, &subject, &score);

                    if authorized.contains(&caller) {
                        // Invariant 2: authorized writes succeed.
                        assert!(result.is_ok(), "authorized write must succeed");

                        // Invariant 1: version strictly increases by one.
                        let after = client.get_score(&subject);
                        let prev = before.map(|s| s.version).unwrap_or(0);
                        assert_eq!(
                            after.version,
                            prev + 1,
                            "version must increase monotonically"
                        );
                        assert_eq!(after.score, *score, "committed score must match write");

                        // Invariant 3: the read is a fully committed value.
                        let reread = client.get_score(&subject);
                        assert_eq!(reread, after, "reads must never be torn");

                        set_expected(&mut expected_version, &subject, after.version);
                    } else {
                        // Invariant 2: unauthorized writes must fail...
                        assert!(result.is_err(), "unauthorized write must fail");
                        // ...and must not mutate state (invariant 3).
                        assert_eq!(
                            client.get_score(&subject),
                            before,
                            "failed write must not change state"
                        );
                    }
                }
                Op::Read(subject) => {
                    // Invariant 3: reads are always consistent with the model.
                    let got = client.get_score(&subject);
                    let want = expected_version
                        .iter()
                        .find(|(s, _)| s == subject)
                        .map(|(_, v)| v);
                    match (got, want) {
                        (Some(entry), Some(v)) => assert_eq!(entry.version, v),
                        (None, None) => {}
                        _ => panic!("read disagrees with shadow model"),
                    }
                }
            }
        }
    }
}

/// Invariant 2 in isolation: no unregistered caller can ever write, across a
/// fuzzed set of callers and subjects.
#[test]
fn prop_unauthorized_writes_are_rejected() {
    for seed in 1..32u64 {
        let env = Env::default();
        env.mock_all_auths();
        let contract_id = env.register_contract(None, RiskScoreRegistry);
        let client = RiskScoreRegistryClient::new(&env, &contract_id);

        let mut rng = Rng::new(seed.wrapping_mul(0xD1B5_4A32_D192_ED03));
        let pool = address_pool(&env, 5);

        // Register exactly one publisher; everyone else is unauthorized.
        let publisher = pool.get(0).unwrap();
        client.register_publisher(&publisher);

        for _ in 0..20 {
            let caller = pool.get(1 + rng.below(4) as u32).unwrap();
            let subject = pool.get(rng.below(5) as u32).unwrap();
            let score = rng.next() as i128;

            let before = client.get_score(&subject);
            assert!(client.try_set_score(&caller, &subject, &score).is_err());
            assert_eq!(client.get_score(&subject), before);
        }
    }
}

/// Invariant 1 in isolation: repeated authorized writes produce a strictly
/// increasing version sequence with no gaps.
#[test]
fn prop_versions_are_strictly_monotonic() {
    for seed in 1..32u64 {
        let env = Env::default();
        env.mock_all_auths();
        let contract_id = env.register_contract(None, RiskScoreRegistry);
        let client = RiskScoreRegistryClient::new(&env, &contract_id);

        let mut rng = Rng::new(seed.wrapping_mul(0x94D0_49BB_1331_11EB));
        let publisher = Address::generate(&env);
        let subject = Address::generate(&env);
        client.register_publisher(&publisher);

        let mut last = 0u32;
        for _ in 0..25 {
            client.set_score(&publisher, &subject, &(rng.next() as i128));
            let entry = client.get_score(&subject).unwrap();
            assert!(entry.version > last, "version must strictly increase");
            assert_eq!(entry.version, last + 1, "versions must not skip");
            last = entry.version;
        }
    }
}

fn set_expected(model: &mut Vec<(Address, u32)>, subject: &Address, version: u32) {
    for i in 0..model.len() {
        let (s, _) = model.get(i).unwrap();
        if &s == subject {
            model.set(i, (subject.clone(), version));
            return;
        }
    }
    model.push_back((subject.clone(), version));
}
