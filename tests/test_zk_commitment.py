"""Tests for the zero-knowledge risk score proof system.

Covers commitment generation, ZK threshold proofs, and verification —
both positive cases and attack / tamper scenarios.
"""

from __future__ import annotations

import copy

import pytest

from detection.zk_commitment import (
    generate_salt,
    h_generator,
    pedersen_commit,
    score_commitment,
    serialize_point,
    deserialize_point,
    verify_commitment,
    add_points,
)
from detection.zk_prover import (
    NUM_BITS,
    ProofError,
    generate_threshold_proof,
    verify_threshold_proof,
)
from py_ecc.bn128 import G1, multiply, eq as bn_eq, curve_order

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WALLET = "GABCDEF123"
FEATURES = {
    "trade_frequency": 15,
    "total_volume": 50000.0,
    "num_counterparties": 3,
    "avg_trade_size": 3333.33,
}
SALT = generate_salt()


@pytest.fixture
def proof_85():
    """A valid proof that score=85 >= threshold=70."""
    _, _, p = generate_threshold_proof(WALLET, 85, FEATURES, SALT, 70)
    return p


# ---------------------------------------------------------------------------
# SHA-256 commitment
# ---------------------------------------------------------------------------


class TestScoreCommitment:
    def test_generate_and_verify(self):
        """Round-trip: commitment verifies against original inputs."""
        P = pedersen_commit(85, 12345)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 85, FEATURES, SALT, px, py)
        assert verify_commitment(WALLET, 85, FEATURES, SALT, px, py, comm)

    def test_different_scores_produce_different_commitments(self):
        """Scores differ ⇒ commitments differ (binding property)."""
        P1 = pedersen_commit(50, 1)
        P2 = pedersen_commit(90, 1)
        c1 = score_commitment(WALLET, 50, FEATURES, SALT, *serialize_point(P1))
        c2 = score_commitment(WALLET, 90, FEATURES, SALT, *serialize_point(P2))
        assert c1 != c2

    def test_different_wallets_produce_different_commitments(self):
        """Wallets differ ⇒ commitments differ."""
        P = pedersen_commit(70, 42)
        px, py = serialize_point(P)
        c1 = score_commitment("GALICE", 70, FEATURES, SALT, px, py)
        c2 = score_commitment("GBOB", 70, FEATURES, SALT, px, py)
        assert c1 != c2

    def test_tampered_score_rejected(self):
        """Changing score after commitment fails verification."""
        P = pedersen_commit(80, 99)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 80, FEATURES, SALT, px, py)
        assert not verify_commitment(WALLET, 81, FEATURES, SALT, px, py, comm)

    def test_tampered_features_rejected(self):
        """Changing features after commitment fails verification."""
        P = pedersen_commit(75, 55)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 75, FEATURES, SALT, px, py)
        bad_features = {**FEATURES, "trade_frequency": 999}
        assert not verify_commitment(WALLET, 75, bad_features, SALT, px, py, comm)

    def test_tampered_salt_rejected(self):
        """Different salt produces different commitment."""
        P = pedersen_commit(60, 77)
        px, py = serialize_point(P)
        salt_a = generate_salt()
        salt_b = generate_salt()
        comm = score_commitment(WALLET, 60, FEATURES, salt_a, px, py)
        assert not verify_commitment(WALLET, 60, FEATURES, salt_b, px, py, comm)

    def test_hex_output_length(self):
        """Commitment is a 64-character hex string (SHA-256)."""
        P = pedersen_commit(100, 0)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 100, FEATURES, SALT, px, py)
        assert len(comm) == 64
        int(comm, 16)  # hex-parseable


# ---------------------------------------------------------------------------
# BN254 / Pedersen commitment helpers
# ---------------------------------------------------------------------------


class TestPedersenCommit:
    def test_point_on_curve(self):
        """Pedersen commitment point lies on BN254."""
        from py_ecc.bn128 import b as bn_b, is_on_curve

        P = pedersen_commit(42, 123456789)
        assert is_on_curve(P, bn_b)

    def test_serialize_round_trip(self):
        """Serialise → deserialise → same point."""
        P = pedersen_commit(99, 888888)
        x, y = serialize_point(P)
        P2 = deserialize_point(x, y)
        assert bn_eq(P, P2)

    def test_h_generator_is_stable(self):
        """H generator is determined once and cached."""
        h1 = h_generator()
        h2 = h_generator()
        assert h1 is h2  # same object (cached)

    def test_h_generator_on_curve(self):
        """H generator lies on BN254."""
        from py_ecc.bn128 import b as bn_b, is_on_curve

        H = h_generator()
        assert is_on_curve(H, bn_b)

    def test_add_points(self):
        """Point addition matches py_ecc built-in."""
        from py_ecc.bn128 import add as bn_add

        a = multiply(G1, 3)
        b = multiply(G1, 5)
        r1 = add_points(a, b)
        r2 = bn_add(a, b)
        assert bn_eq(r1, r2)

    def test_generate_salt_length(self):
        """Salt is always 32 bytes."""
        s = generate_salt()
        assert len(s) == 32
        assert isinstance(s, bytes)


# ---------------------------------------------------------------------------
# ZK threshold proofs
# ---------------------------------------------------------------------------


class TestThresholdProof:
    def test_valid_proof_accepted(self, proof_85):
        """Valid proof for score=85 >= threshold=70 is accepted."""
        assert verify_threshold_proof(70, proof_85, WALLET)

    def test_wrong_threshold_rejected(self, proof_85):
        """Proof for threshold=70 is NOT valid for threshold=95."""
        assert not verify_threshold_proof(95, proof_85, WALLET)

    def test_lower_threshold_rejected_when_proof_bound_to_higher(self, proof_85):
        """Proof for threshold=70 is NOT valid for threshold=50 (different context)."""
        assert not verify_threshold_proof(50, proof_85, WALLET)

    def test_exact_threshold(self):
        """score == threshold is a valid case."""
        _, _, p = generate_threshold_proof(WALLET, 70, FEATURES, SALT, 70)
        assert verify_threshold_proof(70, p, WALLET)

    def test_max_score(self):
        """score == 100 with threshold 0 works."""
        _, _, p = generate_threshold_proof(WALLET, 100, FEATURES, SALT, 0)
        assert verify_threshold_proof(0, p, WALLET)

    def test_min_score(self):
        """score == 0 with threshold 0 works."""
        _, _, p = generate_threshold_proof(WALLET, 0, FEATURES, SALT, 0)
        assert verify_threshold_proof(0, p, WALLET)

    def test_score_below_threshold_raises(self):
        """Generating a proof when score < threshold raises ProofError."""
        with pytest.raises(ProofError, match="below threshold"):
            generate_threshold_proof(WALLET, 30, FEATURES, SALT, 70)

    def test_score_out_of_range_raises(self):
        """Score > 100 raises ProofError."""
        with pytest.raises(ProofError):
            generate_threshold_proof(WALLET, 200, FEATURES, SALT, 50)

    def test_negative_threshold_raises(self):
        """Negative threshold raises ProofError."""
        with pytest.raises(ProofError):
            generate_threshold_proof(WALLET, 50, FEATURES, SALT, -1)

    # ------------------------------------------------------------------
    # Tamper-resistance
    # ------------------------------------------------------------------

    def test_tampered_c0_rejected(self, proof_85):
        """Flipping any c0 invalidates the proof."""
        for i in range(NUM_BITS):
            p = copy.deepcopy(proof_85)
            p["bits"][i]["c0"] = (p["bits"][i]["c0"] + 1) % curve_order
            assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_c1_rejected(self, proof_85):
        """Flipping any c1 invalidates the proof."""
        for i in range(NUM_BITS):
            p = copy.deepcopy(proof_85)
            p["bits"][i]["c1"] = (p["bits"][i]["c1"] + 1) % curve_order
            assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_s0_rejected(self, proof_85):
        """Flipping any s0 invalidates the proof."""
        for i in range(NUM_BITS):
            p = copy.deepcopy(proof_85)
            p["bits"][i]["s0"] = (p["bits"][i]["s0"] + 1) % curve_order
            assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_s1_rejected(self, proof_85):
        """Flipping any s1 invalidates the proof."""
        for i in range(NUM_BITS):
            p = copy.deepcopy(proof_85)
            p["bits"][i]["s1"] = (p["bits"][i]["s1"] + 1) % curve_order
            assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_commit_coords_rejected(self, proof_85):
        """Altering bit commitment coordinates invalidates the proof."""
        p = copy.deepcopy(proof_85)
        p["bits"][0]["commit_x"] = (p["bits"][0]["commit_x"] + 1) % curve_order
        assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_score_commit_rejected(self, proof_85):
        """Altering the top-level Pedersen commitment invalidates the proof."""
        p = copy.deepcopy(proof_85)
        p["score_commit_x"] = (p["score_commit_x"] + 1) % curve_order
        assert not verify_threshold_proof(70, p, WALLET)

    def test_extra_bits_rejected(self, proof_85):
        """Wrong number of bit proofs is rejected."""
        p = copy.deepcopy(proof_85)
        p["bits"] = p["bits"][: NUM_BITS - 1]
        assert not verify_threshold_proof(70, p, WALLET)

    # ------------------------------------------------------------------
    # Structural
    # ------------------------------------------------------------------

    def test_proof_contains_required_keys(self, proof_85):
        """Proof dict has all required structural keys."""
        assert "score_commit_x" in proof_85
        assert "score_commit_y" in proof_85
        assert "bits" in proof_85
        assert len(proof_85["bits"]) == NUM_BITS
        for b in proof_85["bits"]:
            for k in ("commit_x", "commit_y", "c0", "c1", "s0", "s1"):
                assert k in b

    def test_empty_features_dict(self):
        """Proof generation works with empty features dict."""
        _, _, p = generate_threshold_proof(WALLET, 80, {}, SALT, 50)
        assert verify_threshold_proof(50, p, WALLET)

    def test_commitment_includes_pedersen(self):
        """The SHA-256 commitment hex is deterministic given all inputs."""
        from detection.zk_commitment import score_commitment

        P = pedersen_commit(85, 123456)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 85, FEATURES, SALT, px, py)
        # Recompute — must match
        assert comm == score_commitment(WALLET, 85, FEATURES, SALT, px, py)

    # ------------------------------------------------------------------
    # Proof generation returns correct commitment and coordinates
    # ------------------------------------------------------------------

    def test_generate_returns_matching_commitment(self):
        """The commitment returned by generate_threshold_proof is valid."""
        comm, sc, proof = generate_threshold_proof(WALLET, 85, FEATURES, SALT, 70)
        assert verify_commitment(WALLET, 85, FEATURES, SALT, sc[0], sc[1], comm)

    def test_score_commit_coords_match_proof(self):
        """Score commitment coordinates match between return value and proof."""
        _, sc, proof = generate_threshold_proof(WALLET, 75, FEATURES, SALT, 50)
        assert sc[0] == proof["score_commit_x"]
        assert sc[1] == proof["score_commit_y"]

    # ------------------------------------------------------------------
    # Cross-wallet isolation
    # ------------------------------------------------------------------

    def test_different_wallet_rejects_same_proof(self, proof_85):
        """Proof generated for wallet A does not verify for wallet B."""
        assert not verify_threshold_proof(70, proof_85, "GOTHERWALLET")

    # ------------------------------------------------------------------
    # Edge: all thresholds in [0, 100]
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("t", [0, 10, 25, 50, 75, 90, 100])
    def test_all_thresholds_for_score_80(self, t):
        """Score=80 should pass for all thresholds ≤ 80 and fail for > 80."""
        try:
            _, _, p = generate_threshold_proof(WALLET, 80, FEATURES, SALT, t)
            assert t <= 80
            assert verify_threshold_proof(t, p, WALLET)
        except ProofError:
            assert t > 80


# ---------------------------------------------------------------------------
# Malformed / malicious inputs
# ---------------------------------------------------------------------------


class TestMalformedProofs:
    def test_empty_proof_rejected(self):
        """Empty proof dict is rejected."""
        assert not verify_threshold_proof(70, {}, WALLET)

    def test_none_proof_rejected(self):
        """None proof is rejected."""
        assert not verify_threshold_proof(70, None, WALLET)  # type: ignore[arg-type]

    def test_missing_score_commit_rejected(self):
        """Proof missing score_commit fields is rejected."""
        assert not verify_threshold_proof(70, {"bits": []}, WALLET)

    def test_non_dict_proof_rejected(self):
        """Non-dict proof value is rejected."""
        assert not verify_threshold_proof(70, "not a proof", WALLET)  # type: ignore[arg-type]

    def test_wrong_field_types_rejected(self):
        """Proof with wrong field types is rejected."""
        assert not verify_threshold_proof(70, {"score_commit_x": "abc", "score_commit_y": "def", "bits": []}, WALLET)

    def test_bit_count_mismatch_rejected(self, proof_85):
        """Too few or too many bits rejected."""
        p = copy.deepcopy(proof_85)
        p["bits"] = p["bits"][:3]
        assert not verify_threshold_proof(70, p, WALLET)
        p2 = copy.deepcopy(proof_85)
        p2["bits"] = p2["bits"] * 2
        assert not verify_threshold_proof(70, p2, WALLET)
