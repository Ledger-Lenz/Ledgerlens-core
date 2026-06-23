"""Tests for the regulatory compliance export layer (issue #64)."""

import base64
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detection.compliance_exporter import (
    augment_ivms_payload,
    build_ivms_risk_field,
    generate_sar_package,
    get_audit_trail,
)
from detection.risk_score import RiskScore
from detection.sar_narrative import generate_sar_narrative
from detection.storage import save_alerts, save_scores, save_submission

WALLET = "G" + "A" * 55
COMPLIANCE_KEY = "test-compliance-key"


def _score(score, *, asset_pair="XLM/USDC", ts=None):
    return RiskScore(
        wallet=WALLET,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=90,
        timestamp=ts or datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def _seed(db_path):
    save_scores(
        [
            _score(40, ts=datetime(2026, 6, 1, tzinfo=timezone.utc)),
            _score(92, ts=datetime(2026, 6, 5, tzinfo=timezone.utc)),
            _score(60, asset_pair="XLM/EURC", ts=datetime(2026, 6, 4, tzinfo=timezone.utc)),
        ],
        db_path,
    )
    save_alerts(
        [
            {
                "alert_type": "SANDWICH_ATTACK",
                "wallet": WALLET,
                "asset_pair": "XLM/USDC",
                "pool_id": "P1",
                "detail": {"victim": "GVICTIM", "profit_xlm": 100.0},
                "timestamp": "2026-06-03T00:00:00+00:00",
            }
        ],
        db_path=db_path,
    )


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "compliance.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", path)
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", path)
    return path


# ---------------------------------------------------------------------------
# IVMS 101 augmentation
# ---------------------------------------------------------------------------


def test_augment_ivms_payload_injects_risk_fields(db_path):
    _seed(db_path)
    payload = {
        "originator": {"originatorPersons": [{"naturalPerson": {"name": "Alice"}}]},
        "beneficiary": {"beneficiaryPersons": [{"naturalPerson": {"name": "Bob"}}]},
    }
    augmented = augment_ivms_payload(payload, WALLET, db_path=db_path)

    # Original IVMS members are preserved (input not mutated).
    assert "originator" in augmented and "beneficiary" in augmented
    assert "ledgerLensRiskAssessment" not in payload

    risk = augmented["ledgerLensRiskAssessment"]
    assert risk["ledgerlens_score"] == 92.0
    assert risk["risk_level"] == "CRITICAL"
    assert risk["alert_types"] == ["SANDWICH_ATTACK"]
    # evidence_hash is a 64-char hex SHA-256 commitment.
    assert len(risk["evidence_hash"]) == 64
    int(risk["evidence_hash"], 16)


def test_build_ivms_risk_field_no_scores(db_path):
    field = build_ivms_risk_field(WALLET, db_path=db_path)
    assert field.ledgerlens_score == 0.0
    assert field.risk_level == "LOW"
    assert field.alert_types == []


# ---------------------------------------------------------------------------
# SAR package
# ---------------------------------------------------------------------------


def test_generate_sar_package_produces_valid_zip(db_path, tmp_path):
    _seed(db_path)
    out_dir = str(tmp_path / "out")
    zip_path = generate_sar_package(
        WALLET, "2026-06-01T00:00:00+00:00", "2026-06-30T00:00:00+00:00", out_dir, db_path=db_path
    )

    assert os.path.isfile(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        expected = {
            "sar_narrative.txt",
            "evidence/alerts.json",
            "evidence/score_history.csv",
            "evidence/graph_export.gexf",
            "evidence/shap_explanations.json",
            "manifest.json",
        }
        assert expected <= names

        manifest = json.loads(archive.read("manifest.json"))
        # Every manifest hash matches the archived file's actual SHA-256.
        for name, meta in manifest["files"].items():
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            assert actual == meta["sha256"]


def test_sar_narrative_has_no_placeholder_tokens(db_path):
    _seed(db_path)
    narrative = generate_sar_narrative(
        wallet=WALLET,
        start_date="2026-06-01",
        end_date="2026-06-30",
        peak_score=92,
        alerts=[
            {
                "alert_type": "SANDWICH_ATTACK",
                "asset_pair": "XLM/USDC",
                "detail": {"victim": "GVICTIM", "profit_xlm": 100.0},
                "timestamp": "2026-06-03T00:00:00+00:00",
            }
        ],
        volume_xlm=12345.0,
        n_pairs=2,
        cluster_size=3,
        chi_sq=4.2,
        chi_p=0.012,
    )
    assert "{" not in narrative and "}" not in narrative
    assert "CRITICAL" in narrative
    assert WALLET in narrative


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_get_audit_trail_is_chronological(db_path):
    _seed(db_path)
    save_submission(WALLET, "XLM/USDC", 92, "submitted", tx_hash="abc", db_path=db_path)
    trail = get_audit_trail(WALLET, db_path=db_path)

    assert len(trail) >= 4
    timestamps = [e["timestamp"] for e in trail]
    assert timestamps == sorted(timestamps)
    assert {e["event_type"] for e in trail} >= {"RISK_SCORE", "ALERT", "ON_CHAIN_SUBMISSION"}


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setenv("LEDGERLENS_COMPLIANCE_API_KEY", COMPLIANCE_KEY)
    monkeypatch.setenv(
        "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "compliance_api_key", COMPLIANCE_KEY)

    _seed(db_path)
    from api.main import app

    return TestClient(app)


def test_sar_package_endpoint_requires_scope(client):
    body = {
        "wallet": WALLET,
        "start_date": "2026-06-01T00:00:00+00:00",
        "end_date": "2026-06-30T00:00:00+00:00",
    }
    # Without the compliance:read scope -> 403.
    resp = client.post("/compliance/sar-package", json=body)
    assert resp.status_code == 403

    # With the scope -> 200 + a ZIP body.
    resp = client.post(
        "/compliance/sar-package", json=body, headers={"X-LedgerLens-Compliance-Key": COMPLIANCE_KEY}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(__import__("io").BytesIO(resp.content)) as archive:
        assert "manifest.json" in archive.namelist()


def test_ivms_endpoint_returns_risk_block(client):
    resp = client.get(
        f"/compliance/ivms/{WALLET}", headers={"X-LedgerLens-Compliance-Key": COMPLIANCE_KEY}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk_level"] == "CRITICAL"
    assert body["ledgerlens_score"] == 92.0


def test_audit_trail_endpoint_forbidden_without_key(client):
    resp = client.get(f"/compliance/audit-trail/{WALLET}")
    assert resp.status_code == 403


def test_compliance_endpoints_excluded_from_openapi(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert not any(p.startswith("/compliance/") for p in paths)
