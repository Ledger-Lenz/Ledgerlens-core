"""Tests for the enriched /health endpoint."""

import pytest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_health_includes_all_new_fields(client):
    resp = client.get("/health")
    data = resp.json()
    assert "pipeline_last_run_at" in data
    assert "soroban_circuit_status" in data
    assert "webhook_dead_letter_count" in data
    assert "drift_status" in data
    assert "status" in data


def test_health_returns_503_when_drift_drifted(client):
    mock_reports = [{"drift_detected": True, "psi_report": {}, "threshold": 0.20}]
    with patch("api.main.get_drift_reports", return_value=mock_reports):
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["drift_status"] == "drifted"
    assert resp.json()["status"] == "degraded"


def test_health_returns_503_when_soroban_circuit_open(client):
    import api.main as api_main
    # Set the module-level circuit flag and reset afterwards
    api_main.set_soroban_circuit_open(True)
    try:
        resp = client.get("/health")
    finally:
        api_main.set_soroban_circuit_open(False)
    assert resp.status_code == 503
    assert resp.json()["soroban_circuit_status"] == "open"
    assert resp.json()["status"] == "degraded"


def test_health_returns_503_when_dead_letters_present(client):
    dead_letter = MagicMock()
    with patch("api.main.get_dead_letters", return_value=[dead_letter]):
        resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["webhook_dead_letter_count"] > 0
    assert data["status"] == "degraded"


def test_health_200_when_all_ok(client):
    """Health fields are present and circuit is closed / drift ok when patched clean."""
    with patch("api.main.get_dead_letters", return_value=[]), \
         patch("api.main.get_drift_reports", return_value=[{"drift_detected": False}]):
        resp = client.get("/health")
    data = resp.json()
    assert "soroban_circuit_status" in data
    assert data["soroban_circuit_status"] == "closed"
    assert data["webhook_dead_letter_count"] == 0
    assert data["drift_status"] == "ok"
