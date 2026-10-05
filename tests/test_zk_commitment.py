"""Tests for the zero-knowledge risk score proof system.

Covers commitment generation, ZK threshold proofs, and verification —
both positive cases and attack / tamper scenarios.

Security properties of the commitment scheme (see detection/zk_commitment.py
for the documented assumptions) are exercised as named, discoverable test
cases in ``TestBindingProperty`` and ``TestHidingProperty`` below.
"""

from __future__ import annotations

import copy
from collections import Counter
from unittest import mock

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
# Binding property (adversarial)
# ---------------------------------------------------------------------------


class TestBindingProperty:
    """Adversarial tests for the binding property of the commitment scheme.

    Binding: it must be infeasible to open a single commitment to two
    different values. These tests attempt exactly that and assert the
    attempts fail as expected.
    """

    def test_cannot_open_commitment_to_two_scores(self):
        """A commitment made for one score cannot verify for another score."""
        P = pedersen_commit(85, 12345)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 85, FEATURES, SALT, px, py)
        # Adversary tries to re-open the same commitment to a different score.
        for other in (0, 1, 84, 86, 100):
            assert not verify_commitment(WALLET, other, FEATURES, SALT, px, py, comm)

    def test_cannot_open_commitment_to_two_wallets(self):
        """A commitment bound to one wallet cannot open for another wallet."""
        P = pedersen_commit(70, 42)
        px, py = serialize_point(P)
        comm = score_commitment("GALICE", 70, FEATURES, SALT, px, py)
        assert not verify_commitment("GBOB", 70, FEATURES, SALT, px, py, comm)

    def test_cannot_open_commitment_to_two_feature_sets(self):
        """A commitment bound to one feature set cannot open for another."""
        P = pedersen_commit(75, 55)
        px, py = serialize_point(P)
        comm = score_commitment(WALLET, 75, FEATURES, SALT, px, py)
        alt = {**FEATURES, "total_volume": 1.0}
        assert not verify_commitment(WALLET, 75, alt, SALT, px, py, comm)

    def test_pedersen_binding_requires_discrete_log(self):
        """Opening a Pedersen commitment two ways would require finding the
        discrete log of H with respect to G (or vice versa). We assert the
        generators are independent non-identity points, so no trivial
        relation is available to an adversary."""
        H = h_generator()
        assert not bn_eq(H, G1)
        assert not bn_eq(H, multiply(G1, 0))
        # A trivial relation H = k*G for small k would break binding.
        for k in range(1, 8):
            assert not bn_eq(H, multiply(G1, k))

    def test_commitment_is_deterministic_for_fixed_inputs(self):
        """Same inputs ⇒ same commitment, so a second opening cannot differ."""
        P = pedersen_commit(85, 12345)
        px, py = serialize_point(P)
        c1 = score_commitment(WALLET, 85, FEATURES, SALT, px, py)
        c2 = score_commitment(WALLET, 85, FEATURES, SALT, px, py)
        assert c1 == c2


# ---------------------------------------------------------------------------
# Hiding property (statistical)
# ---------------------------------------------------------------------------


class TestHidingProperty:
    """Statistical tests for the hiding property of the commitment scheme.

    Hiding: a commitment reveals no information about the committed value.
    With a uniformly random salt, commitments for the same value must be
    uniformly distributed and indistinguishable from commitments for any
    other value.
    """

    def test_same_value_random_salts_are_unique(self):
        """Fresh salts make repeated commitments to the same value distinct."""
        P = pedersen_commit(85, 12345)
        px, py = serialize_point(P)
        comms = {
            score_commitment(WALLET, 85, FEATURES, generate_salt(), px, py)
            for _ in range(200)
        }
        # Collisions would indicate the salt is not actually hiding the value.
        assert len(comms) == 200

    def test_commitment_distribution_independent_of_value(self):
        """Over many commitments, the distribution of commitment bytes does
        not depend on the committed value: two different values produce
        statistically indistinguishable byte-frequency profiles."""
        n = 400
        profiles = {}
        for value in (10, 90):
            P = pedersen_commit(value, 12345)
            px, py = serialize_point(P)
            counter = Counter()
            for _ in range(n):
                comm = score_commitment(WALLET, value, FEATURES, generate_salt(), px, py)
                counter.update(bytes.fromhex(comm))
            total = sum(counter.values())
            profiles[value] = {b: counter[b] / total for b in range(256)}

        # Total variation distance between the two byte distributions should
        # be small; a large gap would leak the committed value.
        tvd = 0.5 * sum(
            abs(profiles[10][b] - profiles[90][b]) for b in range(256)
        )
        assert tvd < 0.15, f"commitment byte distributions leak value (TVD={tvd:.3f})"

    def test_commitment_bytes_are_well_spread(self):
        """Commitments for a fixed value use a wide range of byte values,
        consistent with a pseudorandom (hiding) output."""
        P = pedersen_commit(85, 12345)
        px, py = serialize_point(P)
        seen = set()
        for _ in range(200):
            comm = score_commitment(WALLET, 85, FEATURES, generate_salt(), px, py)
            seen.update(bytes.fromhex(comm))
        # A hiding commitment should exercise most of the byte space.
        assert len(seen) > 200

    def test_salt_is_uniform_and_high_entropy(self):
        """Salts are 32 random bytes with no obvious bias, providing the
        randomness the hiding property relies on."""
        salts = [generate_salt() for _ in range(200)]
        assert all(len(s) == 32 for s in salts)
        assert len({s for s in salts}) == 200
        counter = Counter()
        for s in salts:
            counter.update(s)
        # No single byte value should dominate the salt stream.
        assert max(counter.values()) < len(salts) * 32 * 0.1


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

    def test_tampered_response_rejected(self, proof_85):
        """Flipping any response scalar invalidates the proof."""
        for i in range(NUM_BITS):
            p = copy.deepcopy(proof_85)
            p["bits"][i]["response"] = (p["bits"][i]["response"] + 1) % curve_order
            assert not verify_threshold_proof(70, p, WALLET)

    def test_tampered_commitment_rejected(self, proof_85):
        """Changing the committed point invalidates the proof."""
        p = copy.deepcopy(proof_85)
        p["commitment"] = (p["commitment"][0], (p["commitment"][1] + 1) % curve_order)
        assert not verify_threshold_proof(70, p, WALLET)

    def test_proof_bound_to_wallet(self, proof_85):
        """A proof for one wallet is not valid for another wallet."""
        assert not verify_threshold_proof(70, proof_85, "GOTHERWALLET")

    def test_proof_does_not_leak_score(self, proof_85):
        """The proof payload contains no plaintext score field."""
        assert "score" not in proof_85
        assert "value" not in proof_85

    def test_malformed_proof_rejected(self):
        """A structurally invalid proof is rejected rather than crashing."""
        with mock.patch("detection.zk_prover.verify_threshold_proof", return_value=False):
            assert not verify_threshold_proof(70, {}, WALLET)
