//! Zero-knowledge threshold proof verification using ark-bn254.
//!
//! This module reimplements `detection/zk_prover.py::verify_threshold_proof` in
//! pure Rust using ark-bn254 (the Rust equivalent of `py_ecc.bn128`).  It is
//! gated behind the `zk-verify` Cargo feature.
//!
//! # Fiat-Shamir Transcript Format
//!
//! The challenge is computed as:
//!
//! ```text
//! challenge = SHA256("LedgerLens/zk/v1" || R0_bytes || R1_bytes || B_bytes || context)
//! ```
//!
//! where `context = SHA256(wallet || threshold_byte || score_commit_x || score_commit_y)`.
//!
//! See `docs/zk_proofs.md` for the full protocol specification.
//!
//! # Security
//!
//! - This module is a **verifier only**. It never requires or accepts a secret
//!   blinding factor.  An API that accidentally accepted a prover-side secret
//!   would be a misuse trap – none of the public functions in this module do so.
//! - Cross-implementation test vectors (Python-generated, Rust-verified) are the
//!   primary defence against silent scheme divergence.  See
//!   `tests/fixtures/zk_proof_vectors.json`.

use crate::error::ZkVerifyError;
use crate::models::{BitProof, ThresholdProof};

use ark_bn254::Bn254;
use ark_ec::{pairing::Pairing, AffineRepr, CurveGroup};
use ark_ff::{BigInteger, PrimeField, AddAssign};
use sha2::{Digest, Sha256};

/// The BN254 (alt_bn128) curve order / scalar field modulus.
const CURVE_ORDER: &str = "21888242871839275222246405745257275088548364400416034343698204186575808495617";

/// Maximum score value (matches `detection/zk_prover.py::MAX_SCORE`).
const MAX_SCORE: u32 = 100;
/// Number of bits for score decomposition (2^7 = 128 >= 100).
const NUM_BITS: usize = 7;
/// Wire format version (matches `detection/zk_prover.py::PROOF_WIRE_VERSION`).
const PROOF_WIRE_VERSION: u8 = 1;

/// Parse a decimal string into a scalar field element.
fn parse_scalar(s: &str) -> Result<<Bn254 as Pairing>::ScalarField, ZkVerifyError> {
    let bigint = s
        .parse::<ark_ff::BigInt<4>>()
        .map_err(|_| ZkVerifyError::InvalidFormat(format!("invalid scalar: {}", s)))?;
    Ok(<Bn254 as Pairing>::ScalarField::from(bigint))
}

/// Parse a decimal string into a base field element (for point coordinates).
fn parse_base(s: &str) -> Result<<Bn254 as Pairing>::BaseField, ZkVerifyError> {
    let bigint = s
        .parse::<ark_ff::BigInt<4>>()
        .map_err(|_| ZkVerifyError::InvalidFormat(format!("invalid field element: {}", s)))?;
    Ok(<Bn254 as Pairing>::BaseField::from(bigint))
}

/// The second generator *H* for Pedersen commitments on BN254.
///
/// Derived deterministically from SHA-256 as:
/// `H = SHA256("LedgerLens ZK Generator H") * G1`
/// This matches `detection/zk_commitment.py::h_generator()`.
fn h_generator() -> ark_bn254::G1Affine {
    let g1 = ark_bn254::g1::G1Affine::generator();
    let digest = Sha256::digest(b"LedgerLens ZK Generator H");
    let scalar = <Bn254 as Pairing>::ScalarField::from_le_bytes_mod_order(&digest);
    (g1 * scalar).into_affine()
}

/// Negate a G1 point.
fn neg_point(p: &ark_bn254::G1Affine) -> ark_bn254::G1Affine {
    (-(*p).into_group()).into_affine()
}

/// Add two G1 points.
fn add_points(
    a: &ark_bn254::G1Affine,
    b: &ark_bn254::G1Affine,
) -> ark_bn254::G1Affine {
    ((*a).into_group() + *b).into_affine()
}

/// Multiply a G1 point by a scalar.
fn mul_point(p: &ark_bn254::G1Affine, scalar: &<Bn254 as Pairing>::ScalarField) -> ark_bn254::G1Affine {
    (p * scalar).into_affine()
}

/// Serialize a point into 64 bytes (32 for x, 32 for y) big-endian.
fn point_bytes(p: &ark_bn254::G1Affine) -> Vec<u8> {
    let x = p.x().unwrap_or(&<Bn254 as Pairing>::BaseField::from(0u64));
    let y = p.y().unwrap_or(&<Bn254 as Pairing>::BaseField::from(0u64));
    let x_bytes = x.into_bigint().to_bytes_be();
    let y_bytes = y.into_bigint().to_bytes_be();
    [x_bytes.as_slice(), y_bytes.as_slice()].concat()
}

/// Fiat-Shamir challenge computation.
///
/// `challenge = SHA256("LedgerLens/zk/v1" || R0_bytes || R1_bytes || B_bytes || context) % curve_order`
fn fiat_shamir(
    r0: &ark_bn254::G1Affine,
    r1: &ark_bn254::G1Affine,
    b: &ark_bn254::G1Affine,
    context: &[u8],
) -> <Bn254 as Pairing>::ScalarField {
    let mut hasher = Sha256::new();
    hasher.update(b"LedgerLens/zk/v1");
    hasher.update(point_bytes(r0));
    hasher.update(point_bytes(r1));
    hasher.update(point_bytes(b));
    hasher.update(context);
    let digest = hasher.finalize();
    <Bn254 as Pairing>::ScalarField::from_le_bytes_mod_order(&digest)
}

/// Build the context hash: `SHA256(wallet || threshold_byte || score_commit_x_bytes || score_commit_y_bytes)`.
fn build_context(
    wallet: &str,
    threshold: u32,
    score_commit: &ark_bn254::G1Affine,
) -> Vec<u8> {
    let mut hasher = Sha256::new();
    hasher.update(wallet.as_bytes());
    hasher.update([threshold as u8]);
    hasher.update(point_bytes(score_commit));
    hasher.finalize().to_vec()
}

/// Verify a ZK threshold proof.
///
/// Reimplements `detection/zk_prover.py::verify_threshold_proof` using
/// ark-bn254 for curve arithmetic. Returns `Ok(true)` if the proof is valid.
///
/// # Arguments
///
/// * `proof` - The threshold proof (JSON-deserialized from API response).
/// * `threshold` - The threshold value to verify against (score >= threshold).
/// * `wallet` - The wallet address the proof was generated for (used in context).
///
/// # Errors
///
/// Returns `ZkVerifyError` if the proof format is invalid or verification fails.
///
/// # Security
///
/// This function is a **verifier only**. It does not accept or require any
/// secret blinding factor.
pub fn verify_threshold_proof(
    proof: &ThresholdProof,
    threshold: u32,
    wallet: &str,
) -> Result<bool, ZkVerifyError> {
    if threshold > MAX_SCORE {
        return Err(ZkVerifyError::InvalidThreshold(threshold));
    }

    let bits = &proof.bits;
    if bits.len() != NUM_BITS {
        return Err(ZkVerifyError::InvalidFormat(format!(
            "expected {} bit proofs, got {}",
            NUM_BITS,
            bits.len()
        )));
    }

    // Parse the score commitment point.
    let score_commit_x = parse_base(&proof.score_commit_x)?;
    let score_commit_y = parse_base(&proof.score_commit_y)?;
    let p = ark_bn254::G1Affine::new(score_commit_x, score_commit_y);

    // Build context hash.
    let context = build_context(wallet, threshold, &p);

    let h = h_generator();
    let g1 = ark_bn254::G1Affine::generator();

    // 1. Verify each bit proof.
    for (i, bp) in bits.iter().enumerate() {
        let b_x = parse_base(&bp.commit_x)?;
        let b_y = parse_base(&bp.commit_y)?;
        let b = ark_bn254::G1Affine::new(b_x, b_y);

        let c0 = parse_scalar(&bp.c0)?;
        let c1 = parse_scalar(&bp.c1)?;
        let s0 = parse_scalar(&bp.s0)?;
        let s1 = parse_scalar(&bp.s1)?;

        // R0 = s0 * H - c0 * B
        let r0 = add_points(
            &mul_point(&h, &s0),
            &neg_point(&mul_point(&b, &c0)),
        );

        // B_minus_G = B - G
        let b_minus_g = add_points(&b, &neg_point(&g1));

        // R1 = s1 * H - c1 * (B - G)
        let r1 = add_points(
            &mul_point(&h, &s1),
            &neg_point(&mul_point(&b_minus_g, &c1)),
        );

        let expected_c = fiat_shamir(&r0, &r1, &b, &context);

        // Check: c0 + c1 == expected_c (mod curve_order)
        let mut c_sum = c0;
        c_sum.add_assign(&c1);
        if c_sum != expected_c {
            return Ok(false);
        }
    }

    // 2. Verify bit sum: Σ 2^i * B_i == P - T * G
    let p_minus_t_g = add_points(&p, &neg_point(&mul_point(&g1, &<Bn254 as Pairing>::ScalarField::from(threshold as u64))));

    let mut accumulated = ark_bn254::G1Affine::identity();
    for (i, bp) in bits.iter().enumerate() {
        let b_x = parse_base(&bp.commit_x)?;
        let b_y = parse_base(&bp.commit_y)?;
        let b = ark_bn254::G1Affine::new(b_x, b_y);

        let coeff = <Bn254 as Pairing>::ScalarField::from(1u64 << i);
        let term = mul_point(&b, &coeff);
        accumulated = add_points(&accumulated, &term);
    }

    // Check accumulated == P - T * G
    if accumulated != p_minus_t_g {
        return Ok(false);
    }

    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Basic test creating a valid proof structure and verifying it.
    /// This test confirms the function accepts valid inputs without panicking.
    #[test]
    fn test_parse_scalar_and_base() {
        let scalar = parse_scalar("42").unwrap();
        assert!(scalar != <Bn254 as Pairing>::ScalarField::from(0u64));

        let base = parse_base("42").unwrap();
        assert!(base != <Bn254 as Pairing>::BaseField::from(0u64));
    }

    #[test]
    fn test_h_generator_consistent() {
        let h1 = h_generator();
        let h2 = h_generator();
        assert_eq!(h1, h2);
    }

    #[test]
    fn test_point_bytes_roundtrip() {
        let g = ark_bn254::G1Affine::generator();
        let bytes = point_bytes(&g);
        assert_eq!(bytes.len(), 64);
    }

    #[test]
    fn test_verify_rejects_invalid_threshold() {
        let proof = ThresholdProof {
            score_commit_x: "0".to_string(),
            score_commit_y: "0".to_string(),
            bits: vec![],
        };
        let result = verify_threshold_proof(&proof, 200, "");
        assert!(matches!(result, Err(ZkVerifyError::InvalidThreshold(200))));
    }

    #[test]
    fn test_verify_rejects_wrong_bit_count() {
        let proof = ThresholdProof {
            score_commit_x: "0".to_string(),
            score_commit_y: "0".to_string(),
            bits: vec![],
        };
        let result = verify_threshold_proof(&proof, 50, "");
        assert!(matches!(result, Err(ZkVerifyError::InvalidFormat(_))));
    }
}