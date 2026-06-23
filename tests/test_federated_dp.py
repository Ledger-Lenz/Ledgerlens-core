"""Differential Privacy tests for the federated learning subsystem.

Covers:
  (a) Client noise variance matches σ = clip_norm × noise_multiplier.
  (b) Client falls back to (ε,δ)-parametrised σ when noise_multiplier=0.
  (c) RDP budget gate fires when projected ε would exceed max_epsilon.
  (d) Audit records include noise_multiplier and dp_delta for every round.
  (e) Cumulative ε grows sub-linearly (RDP tighter than basic composition).
  (f) Server uses σ = clip_norm × noise_multiplier for aggregation noise.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from detection.federated.audit import get_audit_records, get_round_count
from detection.federated.client import FederatedClient
from detection.federated.server import FederatedAggregationServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(server: FederatedAggregationServer) -> tuple[str, Ed25519PrivateKey]:
    import uuid
    pid = str(uuid.uuid4())
    sk = Ed25519PrivateKey.generate()
    pub_der = sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    server.register_participant(pid, pub_der)
    return pid, sk


def _submit(server: FederatedAggregationServer, pid: str, sk: Ed25519PrivateKey,
            labels: np.ndarray, n_samples: int = 100) -> dict:
    payload = json.dumps(
        {
            "participant_id": pid,
            "round_id": server.get_round_id(),
            "soft_labels": labels.tolist(),
            "n_samples": n_samples,
        },
        sort_keys=True,
    ).encode()
    return server.submit_update(pid, labels, n_samples, sk.sign(payload))


def _make_rdp_server(tmp_path, *, noise_multiplier: float, max_epsilon: float,
                     min_participants: int = 2) -> FederatedAggregationServer:
    return FederatedAggregationServer(
        min_participants=min_participants,
        gradient_clip_threshold=1.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,          # ignored when noise_multiplier > 0
        dp_delta=0.0,
        dp_max_epsilon=max_epsilon,
        noise_multiplier=noise_multiplier,
        target_delta=1e-5,
        db_path=str(tmp_path / "audit.db"),
    )


# ---------------------------------------------------------------------------
# (a) Client noise variance: σ = clip_norm × noise_multiplier
# ---------------------------------------------------------------------------

def test_client_noise_variance_matches_nm():
    clip_norm = 2.0
    nm = 1.5
    client = FederatedClient(
        operator_id="op-a",
        gradient_clip_threshold=clip_norm,
        noise_multiplier=nm,
    )
    expected_sigma = clip_norm * nm   # 3.0

    n_samples = 50_000
    np.random.seed(42)
    delta = np.zeros(n_samples)
    noisy = client.inject_dp_noise(delta)

    measured_std = float(np.std(noisy))
    # With 50k samples the std estimate should be within 2% of expected σ.
    assert abs(measured_std - expected_sigma) / expected_sigma < 0.02, (
        f"Expected σ≈{expected_sigma:.3f}, measured σ={measured_std:.3f}"
    )


# ---------------------------------------------------------------------------
# (b) Legacy (ε,δ) path still works when noise_multiplier=0
# ---------------------------------------------------------------------------

def test_client_legacy_path_when_nm_zero():
    clip_norm = 1.0
    epsilon, delta_val = 1.0, 1e-5
    expected_sigma = clip_norm * math.sqrt(2.0 * math.log(1.25 / delta_val)) / epsilon

    client = FederatedClient(
        operator_id="op-b",
        gradient_clip_threshold=clip_norm,
        dp_epsilon=epsilon,
        dp_delta=delta_val,
        noise_multiplier=0.0,
    )
    n_samples = 50_000
    np.random.seed(1)
    noisy = client.inject_dp_noise(np.zeros(n_samples))

    measured_std = float(np.std(noisy))
    assert abs(measured_std - expected_sigma) / expected_sigma < 0.02, (
        f"Expected σ≈{expected_sigma:.3f}, measured σ={measured_std:.3f}"
    )


# ---------------------------------------------------------------------------
# (c) RDP budget gate fires when projected ε exceeds max_epsilon
# ---------------------------------------------------------------------------

def test_rdp_budget_gate_fires_at_correct_round(tmp_path):
    # With noise_multiplier=3.0, target_delta=1e-5:
    #   round 1 ε ≈ 1.39, round 2 ε ≈ 2.03, round 3 ε ≈ 2.54
    # Set max_epsilon=2.2 → gate should fire before round 3.
    nm = 3.0
    max_eps = 2.2
    n = 20

    server = _make_rdp_server(tmp_path, noise_multiplier=nm, max_epsilon=max_eps)

    # Round 1 — must succeed
    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    # Round 2 — must succeed (projected ε ≈ 2.03 < 2.2)
    p3, sk3 = _register(server)
    p4, sk4 = _register(server)
    _submit(server, p3, sk3, np.full(n, 0.5))
    _submit(server, p4, sk4, np.full(n, 0.5))

    # Round 3 — gate must fire (projected ε ≈ 2.54 > 2.2)
    p5, sk5 = _register(server)
    with pytest.raises(RuntimeError, match="Privacy budget exhausted"):
        _submit(server, p5, sk5, np.full(n, 0.5))


# ---------------------------------------------------------------------------
# (d) Audit records include noise_multiplier and dp_delta
# ---------------------------------------------------------------------------

def test_audit_records_include_dp_metadata(tmp_path):
    nm = 1.1
    target_delta = 1e-5
    n = 20

    server = _make_rdp_server(
        tmp_path, noise_multiplier=nm, max_epsilon=100.0, min_participants=2
    )
    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) >= 1, "Expected at least one audit record"

    rec = records[0]
    assert "noise_multiplier" in rec, "Audit record must contain noise_multiplier"
    assert "dp_delta" in rec, "Audit record must contain dp_delta"
    assert abs(rec["noise_multiplier"] - nm) < 1e-9
    assert abs(rec["dp_delta"] - target_delta) < 1e-12


# ---------------------------------------------------------------------------
# (e) Cumulative ε grows sub-linearly (RDP tighter than basic composition)
# ---------------------------------------------------------------------------

def test_rdp_epsilon_sublinear_vs_basic_composition(tmp_path):
    nm = 1.1
    target_delta = 1e-5
    n = 20
    n_rounds = 3

    server = _make_rdp_server(
        tmp_path, noise_multiplier=nm, max_epsilon=100.0, min_participants=2
    )
    for _ in range(n_rounds):
        p1, sk1 = _register(server)
        p2, sk2 = _register(server)
        _submit(server, p1, sk1, np.full(n, 0.5))
        _submit(server, p2, sk2, np.full(n, 0.5))

    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) == n_rounds

    rdp_cumulative = records[0]["cumulative_epsilon"]  # newest first

    # Basic composition: ε_basic = n_rounds × ε_per_round_basic
    # ε_per_round_basic via the Gaussian formula at δ=1e-5
    eps_basic_per_round = math.sqrt(2.0 * math.log(1.25 / target_delta)) / nm
    eps_basic_total = n_rounds * eps_basic_per_round

    assert rdp_cumulative < eps_basic_total, (
        f"RDP ε={rdp_cumulative:.4f} should be < basic composition ε={eps_basic_total:.4f}"
    )


# ---------------------------------------------------------------------------
# (f) Server aggregation noise uses σ = clip_norm × noise_multiplier
# ---------------------------------------------------------------------------

def test_server_aggregation_noise_uses_nm(tmp_path):
    # nm=0.1 → σ=0.1.  Labels at 0.5 → clipping probability ~0%
    # (N(0.5, 0.1) rarely exits [0,1]).  Set max_epsilon=1e9 so the
    # budget gate never fires and we can purely measure the noise scale.
    clip_norm = 1.0
    nm = 0.1
    n = 50_000

    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=clip_norm,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1e9,
        noise_multiplier=nm,
        target_delta=1e-5,
        db_path=str(tmp_path / "audit.db"),
    )

    # Submit labels exactly at 0.5 so FedAvg output = 0.5 before noise.
    # Any deviation from 0.5 in the output is purely server-side noise.
    p1, sk1 = _register(server)
    np.random.seed(7)
    _submit(server, p1, sk1, np.full(n, 0.5), n_samples=100)

    global_labels = server.get_global_soft_labels()
    assert global_labels is not None

    measured_std = float(np.std(global_labels))
    expected_sigma = clip_norm * nm  # 0.1

    # With n=50k and negligible clipping, std estimate should be within 5% of σ.
    assert abs(measured_std - expected_sigma) / expected_sigma < 0.05, (
        f"Expected server noise σ≈{expected_sigma:.3f}, measured std={measured_std:.3f}"
    )


# ---------------------------------------------------------------------------
# (g) get_round_count matches number of completed rounds
# ---------------------------------------------------------------------------

def test_get_round_count_tracks_rounds(tmp_path):
    db = str(tmp_path / "audit.db")
    server = _make_rdp_server(tmp_path, noise_multiplier=1.1, max_epsilon=100.0)
    n = 10

    assert get_round_count(db) == 0

    for _ in range(3):
        p1, sk1 = _register(server)
        p2, sk2 = _register(server)
        _submit(server, p1, sk1, np.full(n, 0.5))
        _submit(server, p2, sk2, np.full(n, 0.5))

    assert get_round_count(db) == 3
