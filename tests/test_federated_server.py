"""Tests for the federated aggregation server.

Covers:
  (a) Server aggregates 3 participant updates correctly using FedAvg formula.
  (b) Participant with 2× sample count receives 2× weight.
  (c) Server rejects update with gradient norm > clip threshold.
  (d) Cosine outlier detection excludes a crafted adversarial update.
  (e) Audit record is created and signed for each round.
"""

import json

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from detection.federated.audit import get_audit_records, verify_record
from detection.federated.server import FederatedAggregationServer


def _make_participant(server: FederatedAggregationServer) -> tuple[str, Ed25519PrivateKey]:
    """Register a new participant and return (participant_id, private_key)."""
    import uuid
    pid = str(uuid.uuid4())
    sk = Ed25519PrivateKey.generate()
    pub_der = sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    server.register_participant(pid, pub_der)
    return pid, sk


def _submit(
    server: FederatedAggregationServer,
    participant_id: str,
    private_key: Ed25519PrivateKey,
    soft_labels: np.ndarray,
    n_samples: int,
) -> dict:
    """Build a signed update and submit it to the server."""
    payload = json.dumps(
        {
            "participant_id": participant_id,
            "round_id": server.get_round_id(),
            "soft_labels": soft_labels.tolist(),
            "n_samples": n_samples,
        },
        sort_keys=True,
    ).encode()
    signature = private_key.sign(payload)
    return server.submit_update(
        participant_id=participant_id,
        soft_labels=soft_labels,
        n_samples=n_samples,
        signature=signature,
    )


# ── (a) Correct FedAvg formula ────────────────────────────────────────────────

def test_fedavg_aggregation_correct(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=3,
        gradient_clip_threshold=1000.0,  # no clipping
        gradient_outlier_threshold=-2.0,  # no outlier exclusion
        dp_epsilon=0.0,                   # no DP noise
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    n = 50  # public dataset size
    # Three participants with equal sample counts
    p1, sk1 = _make_participant(server)
    p2, sk2 = _make_participant(server)
    p3, sk3 = _make_participant(server)

    labels1 = np.full(n, 0.2)
    labels2 = np.full(n, 0.6)
    labels3 = np.full(n, 0.8)

    n_samples = 100
    _submit(server, p1, sk1, labels1, n_samples)
    _submit(server, p2, sk2, labels2, n_samples)
    _submit(server, p3, sk3, labels3, n_samples)

    global_labels = server.get_global_soft_labels()
    assert global_labels is not None

    # FedAvg with equal weights → simple mean = (0.2+0.6+0.8)/3 ≈ 0.5333
    expected = (0.2 + 0.6 + 0.8) / 3.0
    assert np.allclose(global_labels, expected, atol=0.01), (
        f"Expected FedAvg ≈ {expected:.4f}, got {global_labels.mean():.4f}"
    )


# ── (b) 2× sample count → 2× weight ──────────────────────────────────────────

def test_fedavg_double_sample_weight(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    n = 40
    p1, sk1 = _make_participant(server)
    p2, sk2 = _make_participant(server)

    labels1 = np.full(n, 0.2)   # small participant
    labels2 = np.full(n, 0.8)   # large participant (2× samples)

    _submit(server, p1, sk1, labels1, n_samples=100)
    _submit(server, p2, sk2, labels2, n_samples=200)  # 2× samples

    global_labels = server.get_global_soft_labels()
    assert global_labels is not None

    # Weighted average: (100×0.2 + 200×0.8) / 300 = (20+160)/300 = 0.6
    expected = (100 * 0.2 + 200 * 0.8) / 300.0
    assert np.allclose(global_labels, expected, atol=0.01), (
        f"Expected weighted FedAvg ≈ {expected:.4f}, got {global_labels.mean():.4f}"
    )


# ── (c) Reject update with norm > clip threshold ──────────────────────────────

def test_norm_clipping_rejects_large_gradient(tmp_path):
    db = str(tmp_path / "audit.db")
    # Clip threshold is 1.0 so any non-trivial gradient gets clipped.
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=1.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    p1, sk1 = _make_participant(server)

    # Global starts at None → prev = 0.5 for each element.
    # Soft label = 1.0 for each of 100 elements → delta norm = sqrt(100 * 0.25) = 5.0 >> 1.0
    n = 100
    large_labels = np.ones(n)

    _submit(server, p1, sk1, large_labels, n_samples=50)
    global_labels = server.get_global_soft_labels()
    assert global_labels is not None

    # After clipping, the aggregated delta should have L2 norm ≤ clip_threshold
    prev = np.full(n, 0.5)
    actual_delta = global_labels - prev
    assert np.linalg.norm(actual_delta) <= 1.0 + 1e-6, (
        f"Aggregated delta norm {np.linalg.norm(actual_delta):.4f} exceeds clip threshold 1.0"
    )


# ── (d) Cosine outlier detection excludes adversarial update ─────────────────

def test_cosine_outlier_excludes_adversarial(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=0.5,   # strict threshold
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    n = 50
    p1, sk1 = _make_participant(server)
    p2, sk2 = _make_participant(server)
    p3, sk3 = _make_participant(server)

    # Round 1: p1 and p2 contribute honest gradients (labels ≈ 0.7)
    labels_honest = np.full(n, 0.7)
    _submit(server, p1, sk1, labels_honest, n_samples=100)
    _submit(server, p2, sk2, labels_honest, n_samples=100)
    # Force aggregate after round 1 to establish a running mean delta
    server.force_aggregate()

    # Round 2: p3 submits a completely adversarial gradient (opposite direction)
    labels_adversarial = np.full(n, 0.01)  # very different direction from the mean
    status = _submit(server, p3, sk3, labels_adversarial, n_samples=100)
    assert status["accepted"] is False, (
        "Adversarial participant with low cosine similarity should be excluded"
    )


# ── (e) Audit record created and signed per round ─────────────────────────────

def test_audit_record_created_and_signed(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_delta=1e-5,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    from cryptography.hazmat.primitives.serialization import load_der_public_key

    n = 30
    p1, sk1 = _make_participant(server)
    p2, sk2 = _make_participant(server)

    _submit(server, p1, sk1, np.full(n, 0.4), n_samples=80)
    _submit(server, p2, sk2, np.full(n, 0.6), n_samples=80)

    records = get_audit_records(db_path=db)
    assert len(records) >= 1, "Expected at least one audit record after aggregation"

    latest = records[0]
    sig_hex = latest.pop("_signature_hex")
    sig_bytes = bytes.fromhex(sig_hex)

    pub_key = load_der_public_key(server.get_server_public_key_der())
    assert verify_record(latest, sig_bytes, pub_key), "Audit record signature must verify"

    # Participant IDs must be hashed, not plaintext
    for hashed_id in latest["participants"]:
        assert len(hashed_id) == 64, "Participant ID must be a 64-char SHA-256 hex string"
