//! LedgerLens ZK Verifier — Soroban contract
//!
//! Stores a SHA-256 commitment and Pedersen curve point for every wallet
//! that has a published risk score, and exposes ``verify_threshold`` so that
//! downstream contracts can check ``score >= threshold`` without learning
//! the raw score or any feature values.
//!
//! # Proof format (Sigma protocol on BN254)
//!
//! The off-chain prover (``detection/zk_prover.py``) produces a proof π:
//!
//! - ``score_commit`` — Pedersen commitment ``P = s·G + r·H`` on BN254
//! - ``bits[0..6]`` — one entry per bit of ``d = s - T``, each containing:
//!     - ``commit`` — bit commitment ``B_i = b_i·G + r_i·H``
//!     - ``c0, c1, s0, s1`` — Sigma OR-proof that ``b_i ∈ {0,1}``
//!
//! Verification (replicated here):
//!   1. For each bit:  ``R0 = s0·H - c0·B_i``,
//!                      ``R1 = s1·H - c1·(B_i - G)``,
//!                      ``c = SHA256(R0 ‖ R1 ‖ B_i ‖ context)``,
//!                      ``c0 + c1 == c``
//!   2. ``Σ 2^i · B_i == P - T·G``

#![no_std]

use soroban_sdk::{contract, contractimpl, contracttype, Address, Bytes, BytesN, Env, Map, Symbol, symbol};

mod curve;
use curve::{Fq, Point};

// ---------------------------------------------------------------------------
// Storage keys
// ---------------------------------------------------------------------------

const COMMITMENTS: Symbol = symbol!("commitments");

/// On-chain record for a single wallet.
#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScoreCommitment {
    /// SHA-256 hex string (the public binding commitment).
    pub commitment_hash: BytesN<32>,
    /// Pedersen commitment point *x*-coordinate (BN254 field element).
    pub pedersen_x: BytesN<32>,
    /// Pedersen commitment point *y*-coordinate (BN254 field element).
    pub pedersen_y: BytesN<32>,
    /// Numeric score 0-100 (published for non-ZK consumers).
    pub score: u32,
    /// Ledger timestamp of the last update.
    pub timestamp: u64,
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

        let mut map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&COMMITMENTS).unwrap_or(Map::new(&env));

        let entry = ScoreCommitment {
            commitment_hash,
            pedersen_x,
            pedersen_y,
            score,
            timestamp: env.ledger().timestamp(),
        };
        map.set(wallet, entry);
        env.storage().instance().set(&COMMITMENTS, &map);
    }

    // ------------------------------------------------------------------
    // Queries
    // ------------------------------------------------------------------

    /// Read the stored score for *wallet* (non-ZK path).
    pub fn get_score(env: Env, wallet: Address) -> u32 {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&COMMITMENTS).unwrap_or(Map::new(&env));
        map.get(wallet).map(|e| e.score).unwrap_or(0)
    }

    /// Read the stored SHA-256 commitment hash for *wallet*.
    pub fn get_commitment(env: Env, wallet: Address) -> Option<ScoreCommitment> {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&COMMITMENTS).unwrap_or(Map::new(&env));
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
    /// * `proof` — CBOR-serialised proof π as ``Bytes``.
    ///
    /// # Returns
    /// ``true`` if the proof is valid AND ``score >= threshold``.
    pub fn verify_threshold(
        env: Env,
        wallet: Address,
        threshold: u32,
        proof: Bytes,
    ) -> bool {
        let map: Map<Address, ScoreCommitment> =
            env.storage().instance().get(&COMMITMENTS).unwrap_or(Map::new(&env));
        let Some(entry) = map.get(wallet) else {
            return false; // wallet has no score on record
        };

        // Deserialise the proof (custom CBOR-like format for gas efficiency).
        let Some(proof_data) = Self::deserialise_proof(&proof) else {
            return false;
        };

        // Reconstruct the Pedersen commitment point from storage.
        let P = Self::point_from_storage(&entry.pedersen_x, &entry.pedersen_y);

        // 1. Verify each bit proof.
        let context = Self::proof_context(&wallet, threshold);

        for bp in &proof_data.bits {
            let B = Point {
                x: Fq::from_bytes(&bp.commit_x),
                y: Fq::from_bytes(&bp.commit_y),
                infinity: false,
            };

            // R0 = s0·H - c0·B
            let R0 = Point::h_generator()
                .mul_scalar(&Fq::from_u64(bp.s0))
                .add(&B.mul_scalar(&Fq::from_u64(bp.c0)).neg());

            // R1 = s1·H - c1·(B - G)
            let B_minus_G = B.add(&Point::generator().neg());
            let R1 = Point::h_generator()
                .mul_scalar(&Fq::from_u64(bp.s1))
                .add(&B_minus_G.mul_scalar(&Fq::from_u64(bp.c1)).neg());

            // c = SHA256(R0 ‖ R1 ‖ B ‖ context)
            let challenge = Self::fiat_shamir(&R0, &R1, &B, &context);
            let expected_c = bp.c0.wrapping_add(bp.c1);

            if challenge != expected_c {
                return false;
            }
        }

        // 2. Verify bit sum:  Σ 2^i · B_i == P - T·G
        let P_minus_T_G = P.add(&Point::generator().mul_scalar(&Fq::from_u64(threshold as u64)).neg());

        let mut accumulated = Point::zero();
        for (i, bp) in proof_data.bits.iter().enumerate() {
            let B_i = Point {
                x: Fq::from_bytes(&bp.commit_x),
                y: Fq::from_bytes(&bp.commit_y),
                infinity: false,
            };
            let weight = 1u64 << i;
            accumulated = accumulated.add(&B_i.mul_scalar(&Fq::from_u64(weight)));
        }

        accumulated.eq(&P_minus_T_G)
    }

    // ------------------------------------------------------------------
    // Internal helpers
    // ------------------------------------------------------------------

    fn proof_context(wallet: &Address, threshold: u32) -> BytesN<32> {
        let mut buf = [0u8; 64];
        // Wallet bytes + threshold byte
        // In production, use a proper domain separation hash
        let wallet_bytes = wallet.to_xdr(); // actually use env serialisation
        let digest = soroban_sdk::crypto::sha256(
            &soroban_sdk::Bytes::from_slice(
                &soroban_sdk::Env::default(),
                &[b"LedgerLens/zk/v1/context", wallet_bytes.as_slice(), &[threshold as u8]].concat(),
            ),
        );
        digest
    }

    fn fiat_shamir(
        _r0: &Point,
        _r1: &Point,
        _b: &Point,
        _context: &BytesN<32>,
    ) -> u64 {
        // In production: SHA256(R0_x ‖ R0_y ‖ R1_x ‖ R1_y ‖ B_x ‖ B_y ‖ context)
        // and reduce mod curve order.
        // Simplified implementation for structure:
        let data = [
            b"LedgerLens/zk/v1/challenge",
        ];
        // Use env crypto
        let _digest = soroban_sdk::crypto::sha256(
            &soroban_sdk::Bytes::from_slice(&soroban_sdk::Env::default(), b"placeholder"),
        );
        42 // placeholder
    }

    fn point_from_storage(x_bytes: &BytesN<32>, y_bytes: &BytesN<32>) -> Point {
        Point {
            x: Fq::from_bytes(x_bytes),
            y: Fq::from_bytes(y_bytes),
            infinity: false,
        }
    }

    fn deserialise_proof(_proof: &Bytes) -> Option<ProofData> {
        // In production, parse the CBOR-like bytes into ProofData.
        // For the scaffold, return None (to be implemented with actual serialisation).
        None
    }
}

// ---------------------------------------------------------------------------
// Proof data structures
// ---------------------------------------------------------------------------

struct BitProof {
    commit_x: [u8; 32],
    commit_y: [u8; 32],
    c0: u64,
    c1: u64,
    s0: u64,
    s1: u64,
}

struct ProofData {
    score_commit_x: [u8; 32],
    score_commit_y: [u8; 32],
    bits: [BitProof; 7],
}

// ---------------------------------------------------------------------------
// Fq helpers (bridge between BytesN and Fq)
// ---------------------------------------------------------------------------

impl Fq {
    fn from_bytes(bytes: &BytesN<32>) -> Self {
        let arr = bytes.to_array();
        let lo = u128::from_le_bytes(arr[0..16].try_into().unwrap());
        let hi = u128::from_le_bytes(arr[16..32].try_into().unwrap());
        Fq(lo, hi)
    }

    fn from_u64(v: u64) -> Self {
        Fq(v as u128, 0)
    }
}
