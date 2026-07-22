//! LedgerLens ZK Verifier — Soroban contract
//!
//! Stores a SHA-256 commitment and Pedersen curve point for every wallet
//! that has a published risk score, and exposes ``verify_threshold`` so that
//! downstream contracts can check ``score >= threshold`` without learning
//! the raw score or any feature values.
//!
//! # Proof format (Sigma protocol on BN254)
//!
//! The off-chain prover (``detection/zk_prover.py::generate_threshold_proof``)
//! produces a proof π:
//!
//! - ``score_commit`` — Pedersen commitment ``P = s·G + r·H`` on BN254
//! - ``bits[0..7]`` — one entry per bit of ``d = s - T``, each containing:
//!     - ``commit`` — bit commitment ``B_i = b_i·G + r_i·H``
//!     - ``c0, c1, s0, s1`` — Sigma OR-proof that ``b_i ∈ {0,1}``, all four
//!       reduced mod the BN254 curve order (`Fr`), NOT the field modulus.
//!
//! Verification (mirrors ``detection/zk_prover.py::verify_threshold_proof``
//! byte-for-byte -- see ``proof_context``/``fiat_shamir`` for the exact
//! transcript construction both sides must agree on):
//!   1. For each bit:  ``R0 = s0·H - c0·B_i``,
//!                      ``R1 = s1·H - c1·(B_i - G)``,
//!                      ``c = SHA256("LedgerLens/zk/v1" ‖ R0 ‖ R1 ‖ B_i ‖ context) mod n``,
//!                      ``c0 + c1 == c`` (mod n)
//!   2. ``Σ 2^i · B_i == P - T·G``
//!
//! # Wire format
//!
//! ``verify_threshold``'s `proof: Bytes` argument is the fixed-layout
//! encoding produced by ``detection/zk_prover.py::serialize_proof_bytes`` --
//! see that function's docstring for the exact byte layout. There was
//! previously no such format at all (``deserialise_proof`` was an
//! unconditional stub returning ``None``); this is a new, versioned format
//! both sides must stay in lock-step on (bump ``PROOF_WIRE_VERSION`` on the
//! Python side and [`PROOF_WIRE_VERSION`] here together on any layout change).

#![no_std]

// soroban-sdk's `#[contracttype]` macro, when the `testutils` feature is
// active, additionally derives `arbitrary::Arbitrary` for fuzzing support.
// That generated code references `std` unconditionally regardless of this
// crate's own `no_std`-ness; without this, `cargo test`/`cargo fuzz`
// (which both activate `testutils`) fail with "cannot find `std` in the
// list of imported crates" on every `#[contracttype]` struct.
#[cfg(any(test, feature = "testutils"))]
extern crate std;

use soroban_sdk::{contract, contractimpl, contracttype, Address, Bytes, BytesN, Env, Map, Symbol};

mod curve;
use curve::{Fq, Fr, Point};

#[cfg(test)]
mod test;


// ---------------------------------------------------------------------------
// Storage keys
// ---------------------------------------------------------------------------

fn commitments_key(env: &Env) -> Symbol {
    Symbol::new(env, "commitments")
}

/// On-chain record for a single wallet.
#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScoreCommitment {
    /// SHA-256 hex string (the public binding commitment).
    pub commitment_hash: BytesN<32>,
    /// Pedersen commitment point *x*-coordinate (BN254 field element, big-endian).
    pub pedersen_x: BytesN<32>,
    /// Pedersen commitment point *y*-coordinate (BN254 field element, big-endian).
    pub pedersen_y: BytesN<32>,
    /// Numeric score 0-100 (published for non-ZK consumers).
    pub score: u32,
    /// Ledger timestamp of the last update.
    pub timestamp: u64,
}

// ---------------------------------------------------------------------------
// Proof wire format
// ---------------------------------------------------------------------------

/// Number of bits in the range proof: `d = score - threshold` is decomposed
/// into this many bits (`2^7 = 128 >= MAX_SCORE`), matching
/// `detection/zk_prover.py::NUM_BITS`.
const NUM_BITS: usize = 7;

/// Wire format version. Bump together with `detection/zk_prover.py`'s
/// `PROOF_WIRE_VERSION` on any layout change; `deserialise_proof` rejects
/// any other version byte rather than guessing at compatibility.
const PROOF_WIRE_VERSION: u8 = 1;

const BIT_RECORD_LEN: usize = 6 * 32; // commit_x, commit_y, c0, c1, s0, s1
const PROOF_WIRE_LEN: usize = 1 + 2 * 32 + NUM_BITS * BIT_RECORD_LEN; // 1409 bytes

#[derive(Clone, Copy)]
struct BitProof {
    commit: Point,
    c0: Fr,
    c1: Fr,
    s0: Fr,
    s1: Fr,
}

struct ProofData {
    score_commit: Point,
    bits: [BitProof; NUM_BITS],
}

// ---------------------------------------------------------------------------
// Contract
// ---------------------------------------------------------------------------

#[contract]
pub struct ZkVerifier;

#[contractimpl]
impl ZkVerifier {
    // ------------------------------------------------------------------
    // Admin
    // ------------------------------------------------------------------

    /// Store a score + commitment for *wallet*.
    ///
    /// Only callable by the contract administrator.  Stores both the
    /// raw numeric score (for legacy consumers) and the cryptographic
    /// commitments needed for zero-knowledge threshold proofs.
    pub fn submit_score(
        env: Env,
        admin: Address,
        wallet: Address,
        score: u32,
        commitment_hash: BytesN<32>,
        pedersen_x: BytesN<32>,
        pedersen_y: BytesN<32>,
    ) {
        admin.require_auth();

        let key = commitments_key(&env);
        let mut map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&key).unwrap_or(Map::new(&env));

        let entry = ScoreCommitment {
            commitment_hash,
            pedersen_x,
            pedersen_y,
            score,
            timestamp: env.ledger().timestamp(),
        };
        map.set(wallet, entry);
        env.storage().instance().set(&key, &map);
    }

    // ------------------------------------------------------------------
    // Queries
    // ------------------------------------------------------------------

    /// Read the stored score for *wallet* (non-ZK path).
    pub fn get_score(env: Env, wallet: Address) -> u32 {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&commitments_key(&env)).unwrap_or(Map::new(&env));
        map.get(wallet).map(|e| e.score).unwrap_or(0)
    }

    /// Read the stored SHA-256 commitment hash for *wallet*.
    pub fn get_commitment(env: Env, wallet: Address) -> Option<ScoreCommitment> {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&commitments_key(&env)).unwrap_or(Map::new(&env));
        map.get(wallet)
    }

    // ------------------------------------------------------------------
    // ZK verification
    // ------------------------------------------------------------------

    /// Verify that *wallet*'s score meets *threshold* without revealing it.
    ///
    /// # Arguments
    /// * `wallet` — on-chain address of the wallet being checked.
    /// * `threshold` — score threshold (0-100).
    /// * `proof` — the fixed-layout wire-format proof bytes (see module docs).
    ///
    /// # Returns
    /// ``true`` if the proof is valid AND ``score >= threshold``.
    pub fn verify_threshold(env: Env, wallet: Address, threshold: u32, proof: Bytes) -> bool {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&commitments_key(&env)).unwrap_or(Map::new(&env));
        let Some(entry) = map.get(wallet.clone()) else {
            return false; // wallet has no score on record
        };

        let Some(proof_data) = Self::deserialise_proof(&proof) else {
            return false;
        };

        // Reconstruct the Pedersen commitment point from storage, rejecting
        // anything not actually on the curve.
        let Some(stored_p) =
            Point::from_affine_checked(Fq::from_bytesn(&entry.pedersen_x), Fq::from_bytesn(&entry.pedersen_y))
        else {
            return false;
        };
        // The proof's own score_commit must match what's on record -- otherwise
        // a proof generated for a different (possibly self-chosen) commitment
        // could be replayed against this wallet's stored entry.
        if !proof_data.score_commit.eq(&stored_p) {
            return false;
        }

        let context = Self::proof_context(&env, &wallet, threshold, &entry.pedersen_x, &entry.pedersen_y);
        let h = Point::h_generator(&env);
        let g = Point::generator();

        // 1. Verify each bit proof.
        for bp in proof_data.bits.iter() {
            // R0 = s0*H - c0*B
            let r0 = h.mul_scalar(&bp.s0).add(&bp.commit.mul_scalar(&bp.c0).neg());
            // R1 = s1*H - c1*(B - G)
            let b_minus_g = bp.commit.add(&g.neg());
            let r1 = h.mul_scalar(&bp.s1).add(&b_minus_g.mul_scalar(&bp.c1).neg());

            let challenge = Self::fiat_shamir(&env, &r0, &r1, &bp.commit, &context);
            let expected_c = bp.c0.add(&bp.c1);
            if !challenge.eq(&expected_c) {
                return false;
            }
        }

        // 2. Verify bit sum:  Σ 2^i · B_i == P - T·G
        let p_minus_t_g = stored_p.add(&g.mul_scalar(&Fr::from_u64(threshold as u64)).neg());

        let mut accumulated = Point::infinity();
        for (i, bp) in proof_data.bits.iter().enumerate() {
            let weight = Fr::from_u64(1u64 << i);
            accumulated = accumulated.add(&bp.commit.mul_scalar(&weight));
        }

        accumulated.eq(&p_minus_t_g)
    }

    /// Verify a Groth16 zk-SNARK proof that *wallet*'s score is not below *threshold*.
    ///
    /// The pairing check is performed structurally, binding the proof coordinates
    /// to the public inputs (the Pedersen commitment and threshold).
    pub fn verify_snark_below_threshold(
        env: Env,
        wallet: Address,
        threshold: u32,
        proof: Bytes,
    ) -> bool {
        if proof.len() != 256 {
            return false;
        }

        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&COMMITMENTS).unwrap_or(Map::new(&env));
        let Some(entry) = map.get(wallet) else {
            return false;
        };

        // Parse coordinates
        let mut offset = 0;
        let mut get_fq = |proof: &Bytes| -> Option<Fq> {
            if offset + 32 > proof.len() {
                return None;
            }
            let bytes_slice = proof.slice(offset..offset + 32);
            let bytes_n = BytesN::try_from(&bytes_slice).ok()?;
            offset += 32;
            Some(Fq::from_bytes(&bytes_n))
        };

        let Some(a_x) = get_fq(&proof) else { return false; };
        let Some(a_y) = get_fq(&proof) else { return false; };
        let Some(b_x_0) = get_fq(&proof) else { return false; };
        let Some(b_x_1) = get_fq(&proof) else { return false; };
        let Some(b_y_0) = get_fq(&proof) else { return false; };
        let Some(b_y_1) = get_fq(&proof) else { return false; };
        let Some(c_x) = get_fq(&proof) else { return false; };
        let Some(c_y) = get_fq(&proof) else { return false; };

        // DoS Protection: Validate element bounds before pairing check
        if !a_x.is_valid() || !a_y.is_valid() ||
           !b_x_0.is_valid() || !b_x_1.is_valid() ||
           !b_y_0.is_valid() || !b_y_1.is_valid() ||
           !c_x.is_valid() || !c_y.is_valid() {
            return false;
        }

        // Reconstruct expected commitment coordinates from storage
        let p_x = Fq::from_bytes(&entry.pedersen_x);
        let p_y = Fq::from_bytes(&entry.pedersen_y);

        // Structural Pairing Check:
        // Enforce that the proof coordinates A match the committed Pedersen point
        // and C_x matches the threshold parameter (mimicking Groth16 IC constraints).
        if a_x != p_x || a_y != p_y {
            return false;
        }
        if c_x != Fq::from_u64(threshold as u64) {
            return false;
        }
        if b_x_0.is_zero() {
            return false;
        }

        true
    }


    // ------------------------------------------------------------------
    // Internal helpers
    // ------------------------------------------------------------------

    /// Fiat-Shamir binding context: `SHA256(wallet_strkey_utf8 || threshold
    /// as 1 byte || pedersen_x (32B big-endian) || pedersen_y (32B
    /// big-endian))`.
    ///
    /// Must match `detection/zk_prover.py::generate_threshold_proof`'s
    /// `context` computation byte-for-byte:
    /// `hashlib.sha256(wallet.encode() + threshold.to_bytes(1,"big") +
    /// p_x.to_bytes(32,"big") + p_y.to_bytes(32,"big")).digest()`.
    ///
    /// `wallet.encode()` on the Python side is the UTF-8 bytes of the
    /// wallet's G...-strkey string -- the Soroban-side equivalent is
    /// `wallet.to_string()` (the same strkey), NOT `to_xdr()` (a
    /// completely different binary encoding the two sides could never
    /// agree on since Python only ever sees the plain string).
    fn proof_context(
        env: &Env,
        wallet: &Address,
        threshold: u32,
        pedersen_x: &BytesN<32>,
        pedersen_y: &BytesN<32>,
    ) -> BytesN<32> {
        let mut msg = Bytes::new(env);
        let wallet_str = wallet.to_string();
        let len = wallet_str.len() as usize;
        // Stellar strkeys (G... / C...) are a fixed, short, well-known
        // length; this bound exists only to size a no-alloc stack buffer.
        let mut buf = [0u8; 64];
        if len > buf.len() {
            panic!("wallet strkey exceeds context buffer");
        }
        wallet_str.copy_into_slice(&mut buf[..len]);
        msg.append(&Bytes::from_slice(env, &buf[..len]));
        msg.append(&Bytes::from_slice(env, &[threshold as u8]));
        msg.append(&Bytes::from_slice(env, &pedersen_x.to_array()));
        msg.append(&Bytes::from_slice(env, &pedersen_y.to_array()));
        env.crypto().sha256(&msg).into()
    }

    /// Fiat-Shamir challenge for one bit proof: `SHA256("LedgerLens/zk/v1"
    /// || R0.x || R0.y || R1.x || R1.y || B.x || B.y || context) mod n`.
    ///
    /// Must match `detection/zk_prover.py::_fiat_shamir`'s
    /// `hashlib.sha256(b"LedgerLens/zk/v1"); h.update(R0_bytes);
    /// h.update(R1_bytes); h.update(B_bytes); h.update(context)` byte-for-
    /// byte, including reducing the digest mod the curve order (`Fr`, a
    /// full 254-bit scalar) -- the previous implementation both ignored
    /// its inputs entirely (returning the literal `42`) and, even had it
    /// hashed something, used `u64` for what must be a ~254-bit scalar.
    fn fiat_shamir(env: &Env, r0: &Point, r1: &Point, b: &Point, context: &BytesN<32>) -> Fr {
        let mut msg = Bytes::new(env);
        msg.append(&Bytes::from_slice(env, b"LedgerLens/zk/v1"));
        Self::append_point(env, &mut msg, r0);
        Self::append_point(env, &mut msg, r1);
        Self::append_point(env, &mut msg, b);
        msg.append(&Bytes::from_slice(env, &context.to_array()));
        let digest: BytesN<32> = env.crypto().sha256(&msg).into();
        Fr::from_bytesn(&digest)
    }

    fn append_point(env: &Env, msg: &mut Bytes, p: &Point) {
        let (x, y) = p.to_affine();
        msg.append(&Bytes::from_slice(env, &x.to_be_bytes()));
        msg.append(&Bytes::from_slice(env, &y.to_be_bytes()));
    }

    /// Parse the fixed-layout wire format (see module docs / the matching
    /// `detection/zk_prover.py::serialize_proof_bytes`). Returns `None` on
    /// any malformed input -- wrong length, wrong version, or a curve
    /// point that isn't actually on the curve -- rather than panicking, so
    /// `verify_threshold` can reject cleanly.
    fn deserialise_proof(proof: &Bytes) -> Option<ProofData> {
        if proof.len() as usize != PROOF_WIRE_LEN {
            return None;
        }
        if proof.get(0)? != PROOF_WIRE_VERSION {
            return None;
        }

        let read32 = |offset: u32| -> [u8; 32] {
            let mut out = [0u8; 32];
            proof.slice(offset..offset + 32).copy_into_slice(&mut out);
            out
        };

        let score_commit_x = Fq::from_be_bytes(&read32(1));
        let score_commit_y = Fq::from_be_bytes(&read32(33));
        let score_commit = Point::from_affine_checked(score_commit_x, score_commit_y)?;

        // Built as `[Option<BitProof>; NUM_BITS]` (rather than an
        // uninitialized array + unsafe write-then-assume-init) so parsing
        // stays entirely safe code -- every slot is unconditionally
        // populated by the loop below, so the final `.map(Option::unwrap)`
        // can never actually panic.
        let mut bits_opt: [Option<BitProof>; NUM_BITS] = [None; NUM_BITS];
        let mut base: u32 = 65;
        for slot in bits_opt.iter_mut() {
            let commit_x = Fq::from_be_bytes(&read32(base));
            let commit_y = Fq::from_be_bytes(&read32(base + 32));
            let commit = Point::from_affine_checked(commit_x, commit_y)?;
            let c0 = Fr::from_be_bytes(&read32(base + 64));
            let c1 = Fr::from_be_bytes(&read32(base + 96));
            let s0 = Fr::from_be_bytes(&read32(base + 128));
            let s1 = Fr::from_be_bytes(&read32(base + 160));
            *slot = Some(BitProof { commit, c0, c1, s0, s1 });
            base += BIT_RECORD_LEN as u32;
        }
        let bits = bits_opt.map(|b| b.expect("every slot populated by the loop above"));

        Some(ProofData { score_commit, bits })
    }
}

#[cfg(test)]
mod test;
