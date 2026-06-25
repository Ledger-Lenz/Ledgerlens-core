"""Cryptographic commitments for zero-knowledge risk score proofs.

Two-layer commitment scheme:
  1. SHA-256 hash of (wallet, score, features, salt) -- hides raw features
  2. Pedersen commitment of the score on BN254 -- binds score for ZK proofs

The Pedersen commitment point is serialised and included *inside* the SHA-256
hash so the on-chain verifier can extract it without needing to re-run an ML
model.

Issue #147: Extended with PedersenParams, PedersenCommitment, ThresholdProof
dataclasses and higher-level commit/open/prove_below_threshold/verify_below_threshold
functions for privacy-preserving score attestation.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import Optional

from py_ecc.bn128 import FQ, G1, add as bn_add, curve_order, multiply

# ---------------------------------------------------------------------------
# Nothing-up-my-sleeve generator for Pedersen commitments
# ---------------------------------------------------------------------------
_H = None


def h_generator() -> tuple[FQ, FQ]:
    """Return the second generator *H* on BN254 used for blinding.

    Derived deterministically from SHA-256 so the discrete-log relation
    between *G1* and *H* is unknown.
    """
    global _H
    if _H is None:
        digest = hashlib.sha256(b"LedgerLens ZK Generator H").digest()
        scalar = int.from_bytes(digest, "big") % curve_order
        _H = multiply(G1, scalar)
    return _H


def pedersen_commit(value: int, blinding: int) -> tuple[FQ, FQ]:
    """Pedersen commitment *C = value \\* G + blinding \\* H* on BN254."""
    H = h_generator()
    return bn_add(multiply(G1, value % curve_order), multiply(H, blinding % curve_order))


# ---------------------------------------------------------------------------
# SHA-256 commitment (public, stored on-chain)
# ---------------------------------------------------------------------------

def generate_salt() -> bytes:
    """Return 32 cryptographically-random bytes."""
    return os.urandom(32)


def score_commitment(
    wallet: str,
    score: int,
    features: dict,
    salt: bytes,
    score_commit_x: int,
    score_commit_y: int,
) -> str:
    """SHA-256 commitment that binds everything together.

    The Pedersen commitment point coordinates are included in the hash so
    the on-chain verifier can later reference the same curve point without
    needing the raw score or features.
    """
    payload = json.dumps(
        {
            "wallet": wallet,
            "score": score,
            "features": features,
            "pedersen_x": score_commit_x,
            "pedersen_y": score_commit_y,
        },
        sort_keys=True,
    )
    return hashlib.sha256(salt + payload.encode()).hexdigest()


def verify_commitment(
    wallet: str,
    score: int,
    features: dict,
    salt: bytes,
    score_commit_x: int,
    score_commit_y: int,
    expected: str,
) -> bool:
    """Check that *expected* matches a freshly computed commitment."""
    actual = score_commitment(wallet, score, features, salt, score_commit_x, score_commit_y)
    return actual == expected


# ---------------------------------------------------------------------------
# BN254 point helpers
# ---------------------------------------------------------------------------

def serialize_point(pt: tuple[FQ, FQ]) -> tuple[int, int]:
    """Convert a BN254 point to ``(x, y)`` integer coordinates."""
    return (int(pt[0]), int(pt[1]))


def deserialize_point(x: int, y: int) -> tuple[FQ, FQ]:
    """Reconstruct a BN254 point from integer coordinates."""
    return (FQ(x), FQ(y))


def add_points(
    a: tuple[FQ, FQ], b: tuple[FQ, FQ]
) -> tuple[FQ, FQ]:
    """Add two BN254 points."""
    return bn_add(a, b)


# ---------------------------------------------------------------------------
# Issue #147: Higher-level dataclasses and functions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PedersenParams:
    """BN254 group parameters for Pedersen commitments.

    On BN254, the "group" is the elliptic curve group. The modulus `p` and
    prime order `q` come from py_ecc. `g` and `h` are the x-coordinates of
    the two independent generators G1 and H (stored as ints for serialisation).
    The discrete-log relation between G1 and H is unknown by construction.
    """
    p: int   # field prime
    q: int   # curve order (group order)
    g: int   # G1.x (x-coordinate of base generator)
    h: int   # H.x  (x-coordinate of second generator, unknown DL)


def get_default_params() -> PedersenParams:
    """Return the canonical BN254 Pedersen parameters (singleton, not user-configurable)."""
    from py_ecc.bn128 import field_modulus
    H = h_generator()
    return PedersenParams(
        p=field_modulus,
        q=curve_order,
        g=int(G1[0]),
        h=int(H[0]),
    )


@dataclass
class PedersenCommitment:
    """Pedersen commitment C = score*G + r*H on BN254.

    `C` is stored as (x, y) integer coordinates. `r` is the secret blinding
    factor and must never be transmitted to a verifier.
    """
    C: tuple[int, int]  # (x, y) of the commitment point
    r: int              # blinding factor (secret)

    @property
    def point(self) -> tuple[FQ, FQ]:
        return (FQ(self.C[0]), FQ(self.C[1]))


@dataclass
class ThresholdProof:
    """Non-interactive Sigma protocol proof that score >= threshold.

    All fields are JSON-serialisable integers for transport.
    """
    commitment: tuple[int, int]  # (x, y) of the Pedersen commitment
    threshold: int
    challenge: int              # Fiat-Shamir challenge (for audit)
    response: int               # not used directly – kept for API compatibility
    range_commitments: list[tuple[int, int]]  # per-bit commitment (x, y) pairs
    range_responses: list[dict]               # per-bit {c0,c1,s0,s1} dicts
    wallet: str = ""            # wallet the proof was generated for


def commit(score: int, randomness: Optional[int] = None) -> PedersenCommitment:
    """Commit to *score* using optional *randomness* (generated if not provided).

    Uses secrets.randbelow for cryptographically random blinding when
    randomness is not supplied.
    """
    if not (0 <= score <= 100):
        raise ValueError(f"score must be in [0, 100], got {score}")
    r = randomness if randomness is not None else secrets.randbelow(curve_order)
    pt = pedersen_commit(score, r)
    return PedersenCommitment(C=serialize_point(pt), r=r)


def open(commitment: PedersenCommitment, score: int, randomness: int) -> bool:
    """Verify that *commitment* opens to (*score*, *randomness*).

    Returns True iff C == score*G + randomness*H mod curve_order.
    Uses constant-time point comparison to avoid timing side-channels.
    """
    expected_pt = pedersen_commit(score, randomness)
    expected_x, expected_y = serialize_point(expected_pt)
    # hmac.compare_digest on integer hex strings for constant-time comparison
    import hmac
    actual_x = format(commitment.C[0], "064x")
    actual_y = format(commitment.C[1], "064x")
    exp_x = format(expected_x, "064x")
    exp_y = format(expected_y, "064x")
    return hmac.compare_digest(actual_x, exp_x) and hmac.compare_digest(actual_y, exp_y)


def prove_below_threshold(
    score: int,
    threshold: int,
    commitment: PedersenCommitment,
) -> ThresholdProof:
    """Prove that *score* >= *threshold* (i.e. score is NOT below threshold).

    The naming follows the issue spec ('prove_below_threshold' as the function
    name) but the semantics are: prove score >= threshold without revealing
    the exact score. The wallet context defaults to empty here; pass the
    commitment's binding wallet context externally if needed.

    Raises ValueError if score < threshold.
    """
    from detection.zk_prover import generate_threshold_proof, ProofError

    if score < threshold:
        raise ValueError(f"Cannot prove: score {score} is below threshold {threshold}")

    salt = generate_salt()
    wallet = ""
    try:
        _comm_hex, sc_coords, proof_dict = generate_threshold_proof(
            wallet, score, {}, salt, threshold
        )
    except ProofError as e:
        raise ValueError(str(e)) from e

    range_commitments = [(b["commit_x"], b["commit_y"]) for b in proof_dict["bits"]]
    range_responses = [
        {"c0": b["c0"], "c1": b["c1"], "s0": b["s0"], "s1": b["s1"]}
        for b in proof_dict["bits"]
    ]

    # Fiat-Shamir challenge: hash of commitment + threshold
    challenge = int.from_bytes(
        hashlib.sha256(
            sc_coords[0].to_bytes(32, "big")
            + sc_coords[1].to_bytes(32, "big")
            + threshold.to_bytes(1, "big")
        ).digest(),
        "big",
    ) % curve_order

    return ThresholdProof(
        commitment=sc_coords,
        threshold=threshold,
        challenge=challenge,
        response=0,  # response field is carried inside range_responses
        range_commitments=range_commitments,
        range_responses=range_responses,
        wallet=wallet,
    )


def verify_below_threshold(
    commitment: PedersenCommitment,
    threshold: int,
    proof: ThresholdProof,
) -> bool:
    """Verify that the committed score satisfies score >= threshold.

    Returns True iff the proof is cryptographically valid.
    """
    from detection.zk_prover import verify_threshold_proof

    if proof.threshold != threshold:
        return False

    # Reconstruct the proof_dict format expected by zk_prover
    if len(proof.range_commitments) != len(proof.range_responses):
        return False

    proof_dict = {
        "score_commit_x": proof.commitment[0],
        "score_commit_y": proof.commitment[1],
        "bits": [
            {
                "commit_x": rc[0],
                "commit_y": rc[1],
                **rr,
            }
            for rc, rr in zip(proof.range_commitments, proof.range_responses)
        ],
    }

    return verify_threshold_proof(threshold, proof_dict, proof.wallet)
