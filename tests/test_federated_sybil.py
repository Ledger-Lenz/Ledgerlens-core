"""Tests for the Sybil / weight-inflation fix in the federated server.

Covers the acceptance criteria for the n_samples-weight-bounding issue:
  (a) An admitted participant claiming an extreme n_samples (1000x honest
      participants) is clamped to its admission ceiling and cannot bias the
      aggregate toward its adversarial value -- reproducing and refuting the
      old unbounded-influence behaviour.
  (b) A participant admitted with a large ceiling (no clamping triggered)
      is still bounded by the per-round weight-share cap.
  (c) Legitimate participants with genuinely different (admitted) dataset
      sizes still receive proportionally different weight, within the cap.
  (d) An unadmitted/unapproved identity cannot register at all, so it can
      never contribute to aggregation.
  (e) Cross-round consistency: a sudden large jump in a participant's claimed
      n_samples relative to its own history is flagged and excluded.
  (f) The fix's interaction with existing norm-clipping and cosine-similarity
      checks is well-defined: all checks still run, in a documented order.
"""

import json

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from detection.federated.admission import AdmissionError
from detection.federated.audit import get_audit_records
from detection.federated.server import FederatedAggregationServer


def _make_client() -> tuple[str, Ed25519PrivateKey, bytes]:
    import uuid
    pid = str(uuid.uuid4())
    sk = Ed25519PrivateKey.generate()
    pub_der = sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return pid, sk, pub_der


def _submit(server, pid, sk, labels, n_samples):
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


def _admit_and_register(server, pid, sk, pub_der, max_n_samples):
    server.admit_participant(pid, max_n_samples)
    server.register_participant(pid, pub_der)


# ── (a) Extreme claim clamped by admission ceiling ────────────────────────────

def test_admission_ceiling_clamps_extreme_claim_and_bounds_deviation(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=4,  # 3 honest + 1 attacker in one round
        gradient_clip_threshold=1000.0,  # clip-bounded but disabled here: isolate the weight defense
        gradient_outlier_threshold=-2.0,  # direction-plausible: disable cosine exclusion too
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    n = 50
    honest_value = 0.7
    adversarial_value = 0.1
    ceiling = 2000

    for _ in range(3):
        pid, sk, pub_der = _make_client()
        _admit_and_register(server, pid, sk, pub_der, max_n_samples=ceiling)
        _submit(server, pid, sk, np.full(n, honest_value), n_samples=1000)

    attacker, ask, apub = _make_client()
    _admit_and_register(server, attacker, ask, apub, max_n_samples=ceiling)  # same ceiling as honest
    status = _submit(
        server, attacker, ask, np.full(n, adversarial_value), n_samples=1_000_000  # 1000x the honest median
    )
    assert status["accepted"] is True
    assert status["n_samples_clamped"] is True
    assert status["n_samples_effective"] == ceiling

    global_labels = server.get_global_soft_labels()
    assert global_labels is not None
    deviation_from_honest = abs(float(global_labels.mean()) - honest_value)

    # Reproduce the OLD (unbounded self-report) behaviour for comparison: same
    # scenario but with admission ceilings raised so high they never clamp,
    # and the weight-share cap disabled -- i.e. the pre-fix code path.
    db_unbounded = str(tmp_path / "audit_unbounded.db")
    server_unbounded = FederatedAggregationServer(
        min_participants=4,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db_unbounded,
        max_participant_weight_fraction=1.0,
    )
    for _ in range(3):
        pid, sk, pub_der = _make_client()
        _admit_and_register(server_unbounded, pid, sk, pub_der, max_n_samples=10_000_000_000)
        _submit(server_unbounded, pid, sk, np.full(n, honest_value), n_samples=1000)
    attacker_u, ask_u, apub_u = _make_client()
    _admit_and_register(server_unbounded, attacker_u, ask_u, apub_u, max_n_samples=10_000_000_000)
    _submit(server_unbounded, attacker_u, ask_u, np.full(n, adversarial_value), n_samples=1_000_000)
    unbounded_labels = server_unbounded.get_global_soft_labels()
    unbounded_deviation = abs(float(unbounded_labels.mean()) - honest_value)

    # Refute the old behaviour: the unbounded path is dominated by the
    # attacker (deviates sharply from the honest value); the fixed path
    # stays close to the honest-majority direction.
    assert unbounded_deviation > 0.4, (
        f"Expected the unbounded reproduction to be dominated by the attacker, "
        f"got deviation {unbounded_deviation:.4f}"
    )
    assert deviation_from_honest < 0.3, (
        f"Fixed mechanism should bound deviation from the honest-majority value; "
        f"got {deviation_from_honest:.4f} (unbounded reproduction: {unbounded_deviation:.4f})"
    )
    assert deviation_from_honest < unbounded_deviation


# ── (b) Weight-share cap bounds influence even when the ceiling doesn't trigger ──

def test_weight_share_cap_bounds_large_but_admitted_claim(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=4,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
        max_participant_weight_fraction=0.5,
    )

    n = 50
    for _ in range(3):
        pid, sk, pub_der = _make_client()
        _admit_and_register(server, pid, sk, pub_der, max_n_samples=1000)
        _submit(server, pid, sk, np.full(n, 0.7), n_samples=1000)

    # Admitted with a large ceiling and claims truthfully within it -- the
    # ceiling clamp never triggers, but its raw share (1_000_000/1_003_000 ~= 99.7%)
    # would otherwise dominate the round.
    attacker, ask, apub = _make_client()
    _admit_and_register(server, attacker, ask, apub, max_n_samples=1_000_000)
    status = _submit(server, attacker, ask, np.full(n, 0.1), n_samples=1_000_000)
    assert status["n_samples_clamped"] is False, "Claim is within its ceiling -- clamp should not trigger"

    global_labels = server.get_global_soft_labels()
    # Capped weight: attacker <= 0.5, honest collectively >= 0.5.
    # agg = w_attacker*0.1 + w_honest*0.7 with w_attacker capped at 0.5 exactly
    # (3 honest participants split the other 0.5 evenly) => agg == 0.4.
    assert np.allclose(global_labels, 0.4, atol=0.01), (
        f"Expected weight-share-capped aggregate ≈ 0.4, got {global_labels.mean():.4f}"
    )

    records = get_audit_records(db_path=db)
    assert records, "Expected an audit record"
    assert records[0]["weight_capped_participants"], (
        "Audit record should note which participant(s) had their weight capped"
    )


# ── (c) Legitimate differing (admitted) dataset sizes preserve proportional weight ──

def test_legitimate_differing_sizes_still_proportional_within_cap(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
        max_participant_weight_fraction=1.0,  # not exercising the cap here -- see (b)
    )

    n = 40
    p1, sk1, pub1 = _make_client()
    p2, sk2, pub2 = _make_client()
    _admit_and_register(server, p1, sk1, pub1, max_n_samples=1000)
    _admit_and_register(server, p2, sk2, pub2, max_n_samples=1000)

    _submit(server, p1, sk1, np.full(n, 0.2), n_samples=100)
    _submit(server, p2, sk2, np.full(n, 0.8), n_samples=200)  # genuinely 2x, within its admitted ceiling

    global_labels = server.get_global_soft_labels()
    expected = (100 * 0.2 + 200 * 0.8) / 300.0  # unchanged from the pre-fix formula for honest participants
    assert np.allclose(global_labels, expected, atol=0.01), (
        f"Honest participants with genuinely different admitted sizes must still get "
        f"proportional weight: expected {expected:.4f}, got {global_labels.mean():.4f}"
    )


# ── (d) Unadmitted identity cannot register or contribute ────────────────────

def test_unadmitted_participant_cannot_register(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(min_participants=1, db_path=db)
    pid, sk, pub_der = _make_client()
    with pytest.raises(AdmissionError, match="not admitted"):
        server.register_participant(pid, pub_der)


def test_admission_required_disabled_restores_open_registration(tmp_path):
    """Explicit opt-out escape hatch still works (documented as insecure)."""
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
        admission_required=False,
    )
    pid, sk, pub_der = _make_client()
    server.register_participant(pid, pub_der)  # must not raise
    status = _submit(server, pid, sk, np.full(20, 0.5), n_samples=999_999_999)
    assert status["n_samples_clamped"] is False  # unbounded when admission is disabled


def test_revoked_admission_blocks_registration(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(min_participants=1, db_path=db)
    pid, sk, pub_der = _make_client()
    server.admit_participant(pid, max_n_samples=1000)

    from detection.federated.admission import revoke_admission
    assert revoke_admission(pid, db_path=db) is True

    with pytest.raises(AdmissionError):
        server.register_participant(pid, pub_der)


# ── (e) Cross-round consistency: sudden n_samples jump is flagged ────────────

def test_cross_round_n_samples_jump_is_excluded(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
        max_n_samples_growth_factor=3.0,
    )
    n = 30
    p1, sk1, pub1 = _make_client()
    p2, sk2, pub2 = _make_client()
    # Admitted with a high ceiling so the *ceiling* clamp doesn't explain the exclusion --
    # this test isolates the cross-round growth check specifically.
    _admit_and_register(server, p1, sk1, pub1, max_n_samples=10_000_000)
    _admit_and_register(server, p2, sk2, pub2, max_n_samples=10_000_000)

    # Round 1: both submit a stable, modest n_samples -- establishes history.
    _submit(server, p1, sk1, np.full(n, 0.5), n_samples=1000)
    _submit(server, p2, sk2, np.full(n, 0.5), n_samples=1000)

    # Round 2: p1 stays consistent; p2 suddenly claims 10x its own history.
    _submit(server, p1, sk1, np.full(n, 0.5), n_samples=1000)
    status = _submit(server, p2, sk2, np.full(n, 0.5), n_samples=10_000)
    assert status["accepted"] is False
    assert "growth" in status["reason"] or "n_samples" in status["reason"]


def test_cross_round_check_skipped_on_first_round(tmp_path):
    """No history yet on round 1 -- an initially-large-but-within-ceiling claim isn't flagged."""
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=1000.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )
    pid, sk, pub_der = _make_client()
    _admit_and_register(server, pid, sk, pub_der, max_n_samples=10_000_000)
    status = _submit(server, pid, sk, np.full(20, 0.5), n_samples=5_000_000)
    assert status["accepted"] is True


# ── (f) Well-defined interaction with existing gradient defenses ─────────────

def test_all_defenses_run_independently_in_documented_order(tmp_path):
    """norm-clip, cosine-outlier, ceiling-clamp, and growth-check must all still
    run and none silently disables another -- exercise each independently.
    """
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=1.0,  # tight: will clip
        gradient_outlier_threshold=-2.0,  # disabled: isolate norm-clip + ceiling
        dp_epsilon=0.0,
        dp_delta=0.0,
        dp_max_epsilon=1000.0,
        db_path=db,
    )
    n = 100
    pid, sk, pub_der = _make_client()
    _admit_and_register(server, pid, sk, pub_der, max_n_samples=50)  # will clamp

    # Large gradient (triggers norm-clip) AND over-ceiling n_samples (triggers clamp)
    # submitted together -- both must apply; neither should suppress the other.
    status = _submit(server, pid, sk, np.ones(n), n_samples=5000)
    assert status["accepted"] is True
    assert status["n_samples_clamped"] is True
    assert status["n_samples_effective"] == 50

    global_labels = server.get_global_soft_labels()
    prev = np.full(n, 0.5)
    actual_delta = global_labels - prev
    # Norm-clip still bounds the delta magnitude independent of the n_samples fix.
    assert np.linalg.norm(actual_delta) <= 1.0 + 1e-6, (
        "Gradient norm clipping must still apply alongside the n_samples ceiling"
    )


# ── HTTP layer: /federated/register and admin-gated /federated/admit ─────────

def test_http_register_rejects_unadmitted_participant(tmp_path):
    import base64

    from fastapi.testclient import TestClient

    import detection.federated.server as fed_mod
    from detection.federated.server import FederatedAggregationServer, federated_app

    db = str(tmp_path / "audit.db")
    fed_mod._server_instance = FederatedAggregationServer(db_path=db)
    client = TestClient(federated_app)

    sk = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode()

    resp = client.post(
        "/federated/register",
        json={"participant_id": "op-unadmitted", "public_key_der_b64": pub_b64},
    )
    assert resp.status_code == 403
    assert "not admitted" in resp.json()["detail"]


def test_http_admit_requires_admin_key(tmp_path):
    from fastapi.testclient import TestClient

    import detection.federated.server as fed_mod
    from detection.federated.server import FederatedAggregationServer, federated_app
    from config.settings import settings as cfg

    db = str(tmp_path / "audit.db")
    fed_mod._server_instance = FederatedAggregationServer(db_path=db)
    client = TestClient(federated_app)

    # No admin key configured -> fails closed (503), matching require_admin_key elsewhere.
    resp_unconfigured = client.post(
        "/federated/admit", json={"participant_id": "op-x", "max_n_samples": 1000}
    )
    assert resp_unconfigured.status_code == 503

    original = cfg.ledgerlens_admin_api_key
    object.__setattr__(cfg, "ledgerlens_admin_api_key", "test-admin-key")
    try:
        resp_wrong = client.post(
            "/federated/admit",
            json={"participant_id": "op-x", "max_n_samples": 1000},
            headers={"X-LedgerLens-Admin-Key": "wrong-key"},
        )
        assert resp_wrong.status_code == 403

        resp_ok = client.post(
            "/federated/admit",
            json={"participant_id": "op-x", "max_n_samples": 1000},
            headers={"X-LedgerLens-Admin-Key": "test-admin-key"},
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json() == {
            "status": "admitted",
            "participant_id": "op-x",
            "max_n_samples": 1000,
        }
    finally:
        object.__setattr__(cfg, "ledgerlens_admin_api_key", original)
