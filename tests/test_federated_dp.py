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
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=max_epsilon,
        noise_multiplier=noise_multiplier,
        target_delta=1e-5,
        db_path=str(tmp_path / "audit.db"),
    )


def test_client_noise_variance_matches_nm():
    clip_norm = 2.0
    nm = 1.5
    client = FederatedClient(
        operator_id="op-a",
        gradient_clip_threshold=clip_norm,
        noise_multiplier=nm,
    )
    expected_sigma = clip_norm * nm

    np.random.seed(42)
    noisy = client.inject_dp_noise(np.zeros(50_000))

    measured_std = float(np.std(noisy))
    assert abs(measured_std - expected_sigma) / expected_sigma < 0.02, (
        f"Expected σ≈{expected_sigma:.3f}, measured σ={measured_std:.3f}"
    )


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
    np.random.seed(1)
    noisy = client.inject_dp_noise(np.zeros(50_000))

    measured_std = float(np.std(noisy))
    assert abs(measured_std - expected_sigma) / expected_sigma < 0.02, (
        f"Expected σ≈{expected_sigma:.3f}, measured σ={measured_std:.3f}"
    )


def test_rdp_budget_gate_fires_at_correct_round(tmp_path):
    # nm=3.0, δ=1e-5: ε(1)≈1.39, ε(2)≈2.03, ε(3)≈2.54
    # max_epsilon=2.2 → rounds 1 and 2 succeed, round 3 is rejected
    nm = 3.0
    n = 20

    server = _make_rdp_server(tmp_path, noise_multiplier=nm, max_epsilon=2.2)

    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    p3, sk3 = _register(server)
    p4, sk4 = _register(server)
    _submit(server, p3, sk3, np.full(n, 0.5))
    _submit(server, p4, sk4, np.full(n, 0.5))

    p5, sk5 = _register(server)
    with pytest.raises(RuntimeError, match="Privacy budget exhausted"):
        _submit(server, p5, sk5, np.full(n, 0.5))


def test_audit_records_include_dp_metadata(tmp_path):
    nm = 1.1
    target_delta = 1e-5
    n = 20

    server = _make_rdp_server(tmp_path, noise_multiplier=nm, max_epsilon=100.0)
    p1, sk1 = _register(server)
    p2, sk2 = _register(server)
    _submit(server, p1, sk1, np.full(n, 0.5))
    _submit(server, p2, sk2, np.full(n, 0.5))

    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) >= 1

    rec = records[0]
    assert "noise_multiplier" in rec
    assert "dp_delta" in rec
    assert abs(rec["noise_multiplier"] - nm) < 1e-9
    assert abs(rec["dp_delta"] - target_delta) < 1e-12


def test_rdp_epsilon_sublinear_vs_basic_composition(tmp_path):
    nm = 1.1
    target_delta = 1e-5
    n = 20
    n_rounds = 3

    server = _make_rdp_server(tmp_path, noise_multiplier=nm, max_epsilon=100.0)
    for _ in range(n_rounds):
        p1, sk1 = _register(server)
        p2, sk2 = _register(server)
        _submit(server, p1, sk1, np.full(n, 0.5))
        _submit(server, p2, sk2, np.full(n, 0.5))

    records = get_audit_records(db_path=str(tmp_path / "audit.db"))
    assert len(records) == n_rounds

    rdp_cumulative = records[0]["cumulative_epsilon"]
    eps_basic_total = n_rounds * math.sqrt(2.0 * math.log(1.25 / target_delta)) / nm

    assert rdp_cumulative < eps_basic_total, (
        f"RDP ε={rdp_cumulative:.4f} should be < basic composition ε={eps_basic_total:.4f}"
    )


def test_server_aggregation_noise_uses_nm(tmp_path):
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

    p1, sk1 = _register(server)
    np.random.seed(7)
    _submit(server, p1, sk1, np.full(n, 0.5), n_samples=100)

    global_labels = server.get_global_soft_labels()
    assert global_labels is not None

    measured_std = float(np.std(global_labels))
    expected_sigma = clip_norm * nm

    assert abs(measured_std - expected_sigma) / expected_sigma < 0.05, (
        f"Expected server noise σ≈{expected_sigma:.3f}, measured std={measured_std:.3f}"
    )


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
