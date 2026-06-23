"""Zero-knowledge threshold proof generator for risk scores.

Uses a Sigma protocol on BN254 to prove that a Pedersen-committed score
satisfies ``score >= threshold`` without revealing the score or any raw
feature values.

The proof is non-interactive via the Fiat-Shamir heuristic.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from py_ecc.bn128 import FQ, G1, curve_order, neg as bn_neg, multiply, add as bn_add

from detection.zk_commitment import (
    generate_salt,
    h_generator,
    pedersen_commit,
    score_commitment,
    serialize_point,
)

MAX_SCORE = 100
NUM_BITS = 7  # 2^7 = 128 >= 100


class ProofError(Exception):
    """Raised when proof generation or verification fails."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _mod(a: int) -> int:
    return a % curve_order


def _rand_scalar() -> int:
    return int.from_bytes(os.urandom(32), "big") % curve_order


def _fiat_shamir(*parts: bytes) -> int:
    """Hash arbitrary byte strings into a scalar via Fiat-Shamir."""
    h = hashlib.sha256(b"LedgerLens/zk/v1")
    for p in parts:
        h.update(p)
    return int.from_bytes(h.digest(), "big") % curve_order


def _point_bytes(pt: tuple[FQ, FQ]) -> bytes:
    x, y = int(pt[0]), int(pt[1])
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _serialize_proof(
    score_commit: tuple[FQ, FQ],
    bit_commits: list[tuple[FQ, FQ]],
    bit_proofs: list[dict[str, int]],
) -> dict[str, Any]:
    return {
        "score_commit_x": int(score_commit[0]),
        "score_commit_y": int(score_commit[1]),
        "bits": [
            {
                "commit_x": int(bc[0]),
                "commit_y": int(bc[1]),
                "c0": bp["c0"],
                "c1": bp["c1"],
                "s0": bp["s0"],
                "s1": bp["s1"],
            }
            for bc, bp in zip(bit_commits, bit_proofs)
        ],
    }


# ---------------------------------------------------------------------------
# Bit-proof generation (Sigma protocol for b ∈ {0, 1})
# ---------------------------------------------------------------------------

def _prove_bit(
    bit: int,
    blinding: int,
    B: tuple[FQ, FQ],
    context: bytes,
) -> dict[str, int]:
    """Non-interactive Sigma OR-proof that *B* commits to 0 or 1.

    The prover knows *(bit, blinding)* such that
    ``B = bit * G + blinding * H`` with ``bit ∈ {0, 1}``.

    Returns ``{c0, c1, s0, s1}``.
    """
    H = h_generator()

    if bit == 0:
        # Real proof for statement 0 (B = r*H), simulated for statement 1
        c1 = _rand_scalar()
        s1 = _rand_scalar()
        t0 = _rand_scalar()
        s0 = _rand_scalar()

        R0 = multiply(H, t0)  # t0 * H

        # R1 = s1 * H - c1 * (B - G)
        B_minus_G = bn_add(B, bn_neg(G1))
        term1 = multiply(H, s1)
        term2 = multiply(B_minus_G, c1)
        R1 = bn_add(term1, bn_neg(term2))

        c = _fiat_shamir(
            _point_bytes(R0),
            _point_bytes(R1),
            _point_bytes(B),
            context,
        )
        c0 = _mod(c - c1)
        s0 = _mod(t0 + c0 * blinding)

    else:
        # bit == 1: Real proof for statement 1 (B - G = r*H), simulated for statement 0
        c0 = _rand_scalar()
        s0 = _rand_scalar()
        t1 = _rand_scalar()
        s1 = _rand_scalar()

        # R0 = s0 * H - c0 * B  (simulated)
        term0_r0 = multiply(H, s0)
        term0_b = multiply(B, c0)
        R0 = bn_add(term0_r0, bn_neg(term0_b))

        R1 = multiply(H, t1)  # t1 * H  (real)

        c = _fiat_shamir(
            _point_bytes(R0),
            _point_bytes(R1),
            _point_bytes(B),
            context,
        )
        c1 = _mod(c - c0)
        s1 = _mod(t1 + c1 * blinding)

    return {"c0": c0, "c1": c1, "s0": s0, "s1": s1}


# ---------------------------------------------------------------------------
# Public API: threshold proof generation
# ---------------------------------------------------------------------------

def generate_threshold_proof(
    wallet: str,
    score: int,
    features: dict,
    salt: bytes,
    threshold: int,
    _rng_seed: int | None = None,
) -> tuple[str, tuple[int, int], dict[str, Any]]:
    """Generate a ZK proof that ``score >= threshold``.

    Returns
    -------
    (commitment_hex, score_commit_coords, proof_dict)
        *commitment_hex* — the SHA-256 commitment to be stored on-chain.
        *score_commit_coords* — ``(x, y)`` of the Pedersen commitment.
        *proof_dict* — the serialised proof for ``verify_threshold_proof``.
    """
    if not (0 <= score <= MAX_SCORE):
        raise ProofError(f"Score must be 0-{MAX_SCORE}, got {score}")
    if threshold < 0:
        raise ProofError("Threshold must be non-negative")
    if score < threshold:
        raise ProofError("Cannot generate proof: score below threshold")

    d = score - threshold

    # 1. Decompose d into bits
    bits = [(d >> i) & 1 for i in range(NUM_BITS)]

    # 2. Generate bit commitments with random blindings
    r_i_list = [_rand_scalar() for _ in range(NUM_BITS)]
    H = h_generator()
    bit_commits: list[tuple[FQ, FQ]] = []
    for b_i, r_i in zip(bits, r_i_list):
        C_i = bn_add(multiply(G1, b_i), multiply(H, r_i))
        bit_commits.append(C_i)

    # 3. The Pedersen commitment blinding factor is the weighted sum of r_i
    r = sum((1 << i) * r_i_list[i] for i in range(NUM_BITS)) % curve_order
    P = pedersen_commit(score, r)

    # 4. Generate bit proofs — context uses only public values
    p_x_proof, p_y_proof = serialize_point(P)
    context = hashlib.sha256(
        wallet.encode()
        + threshold.to_bytes(1, "big")
        + p_x_proof.to_bytes(32, "big")
        + p_y_proof.to_bytes(32, "big")
    ).digest()

    bit_proofs: list[dict[str, int]] = []
    for b_i, r_i, C_i in zip(bits, r_i_list, bit_commits):
        bp = _prove_bit(b_i, r_i, C_i, context)
        bit_proofs.append(bp)

    # 5. Compute SHA-256 commitment (includes Pedersen point in hash)
    p_x, p_y = serialize_point(P)
    comm = score_commitment(wallet, score, features, salt, p_x, p_y)

    # 6. Serialize proof
    proof = _serialize_proof(P, bit_commits, bit_proofs)

    return comm, (p_x, p_y), proof


# ---------------------------------------------------------------------------
# Public API: proof verification (off-chain mirror of Soroban logic)
# ---------------------------------------------------------------------------

def verify_threshold_proof(
    threshold: int,
    proof: dict[str, Any],
    context_wallet: str = "",
) -> bool:
    """Verify a ZK threshold proof (off-chain equivalent).

    Accepts the same proof format that the Soroban verifier contract
    expects.  Returns ``True`` iff the proof is valid.
    """
    try:
        P = (FQ(proof["score_commit_x"]), FQ(proof["score_commit_y"]))
        bits_data = proof["bits"]
        H = h_generator()

        if len(bits_data) != NUM_BITS:
            return False

        p_x = proof["score_commit_x"]
        p_y = proof["score_commit_y"]
        context = hashlib.sha256(
            context_wallet.encode()
            + threshold.to_bytes(1, "big")
            + p_x.to_bytes(32, "big")
            + p_y.to_bytes(32, "big")
        ).digest()

        # 1. Verify each bit proof
        for i, bd in enumerate(bits_data):
            B = (FQ(bd["commit_x"]), FQ(bd["commit_y"]))
            c0, c1, s0, s1 = bd["c0"], bd["c1"], bd["s0"], bd["s1"]

            # R0 = s0 * H - c0 * B
            R0 = bn_add(multiply(H, s0), bn_neg(multiply(B, c0)))
            # R1 = s1 * H - c1 * (B - G)
            B_minus_G = bn_add(B, bn_neg(G1))
            R1 = bn_add(multiply(H, s1), bn_neg(multiply(B_minus_G, c1)))

            expected_c = _fiat_shamir(
                _point_bytes(R0),
                _point_bytes(R1),
                _point_bytes(B),
                context,
            )
            if _mod(c0 + c1) != expected_c:
                return False

        # 2. Verify bit sum:  Σ 2^i * B_i == P - T * G
        P_minus_T_G = bn_add(P, bn_neg(multiply(G1, threshold)))
        accumulated = multiply(G1, 0)  # point at infinity
        for i, bd in enumerate(bits_data):
            B_i = (FQ(bd["commit_x"]), FQ(bd["commit_y"]))
            term = multiply(B_i, 1 << i)
            accumulated = bn_add(accumulated, term)

        # Check accumulated == P - T * G
        from py_ecc.bn128 import eq as bn_eq

        if not bn_eq(accumulated, P_minus_T_G):
            return False

        return True

    except (KeyError, TypeError, ValueError):
        return False
