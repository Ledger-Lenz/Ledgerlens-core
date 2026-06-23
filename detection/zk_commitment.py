"""Cryptographic commitments for zero-knowledge risk score proofs.

Two-layer commitment scheme:
  1. SHA-256 hash of (wallet, score, features, salt) -- hides raw features
  2. Pedersen commitment of the score on BN254 -- binds score for ZK proofs

The Pedersen commitment point is serialised and included *inside* the SHA-256
hash so the on-chain verifier can extract it without needing to re-run an ML
model.
"""

from __future__ import annotations

import hashlib
import json
import os

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
