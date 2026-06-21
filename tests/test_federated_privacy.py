"""Tests for federated learning privacy guarantees.

Covers:
  (a) Privacy budget accumulation halts the round when FEDERATED_DP_MAX_EPSILON
      is exceeded.
  (b) Participant identifiers in the audit log are SHA-256 hashes, not plaintext.
"""

import hashlib
import json

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from detection.federated.audit import get_audit_records
from detection.federated.server import FederatedAggregationServer


def _register(server: FederatedAggregationServer) -> tuple[str, Ed25519PrivateKey]:
    import uuid
    pid = str(uuid.uuid4())
    sk = Ed25519PrivateKey.generate()
    pub_der = sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    server.register_participant(pid, pub_der)
    return pid, sk


def _submit(server, pid, sk, labels, n_samples=100):
    payload = json.dumps(
        {
            "participant_id": pid,
            "round_id": server.get_round_id(),
            "soft_labels": labels.tolist(),
            "n_samples": n_samples,
        },
        sort_keys=True,
    ).encode()
    sig = sk.sign(payload)
    return server.submit_update(pid, labels, n_samples, sig)


# ── (a) Budget exhaustion halts rounds ────────────────────────────────────────

def test_privacy_budget_halts_when_exhausted(tmp_path):
    db = str(tmp_path / "audit.db")
    # One round costs ε=1.0; max is 2.0 → only 2 rounds allowed.
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_delta=1e-5,
        dp_max_epsilon=2.0,
        db_path=db,
    )

    n = 20
    # Round 1 — should succeed
    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    # Round 2 — cumulative now at 2.0, which equals max (budget exhausted)
    p3, sk3 = _register(server)
    p4, sk4 = _register(server)
    _submit(server, p3, sk3, np.full(n, 0.5))
    _submit(server, p4, sk4, np.full(n, 0.5))

    # Round 3 — must raise RuntimeError (budget exhausted)
    p5, sk5 = _register(server)
    with pytest.raises(RuntimeError, match="Privacy budget exhausted"):
        _submit(server, p5, sk5, np.full(n, 0.5))


# ── (b) Participant IDs in audit log are SHA-256 hashes ───────────────────────

def test_participant_ids_are_hashed_in_audit_log(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_delta=1e-5,
        dp_max_epsilon=100.0,
        db_path=db,
    )

    n = 20
    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    records = get_audit_records(db_path=db)
    assert len(records) >= 1

    for rec in records:
        for hashed_id in rec.get("participants", []):
            # Must be a 64-character SHA-256 hex digest
            assert len(hashed_id) == 64, f"Expected 64-char hash, got: {hashed_id!r}"
            # Must not be the plaintext participant ID
            assert hashed_id != p1, "Plaintext participant ID must not appear in audit log"
            assert hashed_id != p2, "Plaintext participant ID must not appear in audit log"

        # Verify the hash is actually a SHA-256 of the original ID
        expected_hash_p1 = hashlib.sha256(p1.encode()).hexdigest()
        expected_hash_p2 = hashlib.sha256(p2.encode()).hexdigest()
        assert expected_hash_p1 in rec.get("participants", [])
        assert expected_hash_p2 in rec.get("participants", [])
