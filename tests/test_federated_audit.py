"""Tests for the federated audit log.

Covers:
  (a) Audit records can be verified using the server's public key.
  (b) A tampered record fails signature verification.
  (c) Cumulative ε is tracked correctly across 5 rounds.
"""

import json

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

from detection.federated.audit import (
    build_record,
    get_audit_records,
    get_cumulative_epsilon,
    sign_record,
    verify_record,
)
from detection.federated.server import FederatedAggregationServer


def _make_server(tmp_path, min_participants=2, dp_epsilon=1.0) -> FederatedAggregationServer:
    db = str(tmp_path / "audit.db")
    return FederatedAggregationServer(
        min_participants=min_participants,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=dp_epsilon,
        dp_delta=1e-5,
        dp_max_epsilon=1000.0,
        db_path=db,
    )


def _register_and_submit(
    server: FederatedAggregationServer,
    soft_labels: np.ndarray,
    n_samples: int = 100,
    seed: int = 0,
) -> None:
    import uuid
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pid = str(uuid.uuid4())
    sk = Ed25519PrivateKey.generate()
    pub_der = sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    server.register_participant(pid, pub_der)

    payload = json.dumps(
        {
            "participant_id": pid,
            "round_id": server.get_round_id(),
            "soft_labels": soft_labels.tolist(),
            "n_samples": n_samples,
        },
        sort_keys=True,
    ).encode()
    sig = sk.sign(payload)
    server.submit_update(pid, soft_labels, n_samples, sig)


# ── (a) Audit records verifiable with server public key ───────────────────────

def test_audit_records_verify_with_server_public_key(tmp_path):
    server = _make_server(tmp_path, min_participants=2)
    n = 40
    _register_and_submit(server, np.full(n, 0.4))
    _register_and_submit(server, np.full(n, 0.6))

    pub_key = load_der_public_key(server.get_server_public_key_der())
    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) >= 1

    for rec in records:
        sig_hex = rec.pop("_signature_hex")
        sig_bytes = bytes.fromhex(sig_hex)
        assert verify_record(rec, sig_bytes, pub_key), "Valid record must verify"


# ── (b) Tampered record fails signature verification ──────────────────────────

def test_tampered_record_fails_verification(tmp_path):
    server = _make_server(tmp_path, min_participants=2)
    n = 40
    _register_and_submit(server, np.full(n, 0.4))
    _register_and_submit(server, np.full(n, 0.6))

    pub_key = load_der_public_key(server.get_server_public_key_der())
    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) >= 1

    rec = records[0]
    sig_hex = rec.pop("_signature_hex")
    sig_bytes = bytes.fromhex(sig_hex)

    # Tamper: change the aggregated norm
    rec["aggregated_update_norm"] = 9999.0
    assert not verify_record(rec, sig_bytes, pub_key), (
        "Tampered record must fail signature verification"
    )


# ── (c) Cumulative ε tracked correctly across 5 rounds ───────────────────────

def test_cumulative_epsilon_tracked_across_rounds(tmp_path):
    db = str(tmp_path / "audit.db")
    epsilon_per_round = 0.5
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=epsilon_per_round,
        dp_delta=1e-5,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    n = 20
    for _ in range(5):
        _register_and_submit(server, np.random.rand(n))
        _register_and_submit(server, np.random.rand(n))

    # 5 rounds × epsilon_per_round
    expected_cumulative = 5 * epsilon_per_round
    actual = get_cumulative_epsilon(db_path=db)
    assert abs(actual - expected_cumulative) < 1e-6, (
        f"Expected cumulative ε={expected_cumulative:.2f}, got {actual:.2f}"
    )


# ── Extra: standalone sign/verify round-trip ─────────────────────────────────

def test_sign_verify_roundtrip():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    record = build_record(
        round_id="test-round",
        participant_ids=["alice", "bob"],
        aggregated_update_norm=3.14,
        dp_epsilon_consumed=1.0,
        cumulative_epsilon=5.0,
    )
    sig = sign_record(record, sk)
    assert verify_record(record, sig, pk)
