from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from api.main import app
from config.settings import settings

def test_slo_status_unauthorized():
    client = TestClient(app)
    old_key = settings.admin_api_key
    object.__setattr__(settings, "ledgerlens_admin_api_key", "test-admin-key")
    try:
        response = client.get("/v1/admin/slo-status")
        assert response.status_code == 401

        response = client.get("/v1/admin/slo-status", headers={"X-LedgerLens-Admin-Key": "wrong"})
        assert response.status_code == 403
    finally:
        object.__setattr__(settings, "ledgerlens_admin_api_key", old_key)

def test_slo_status_authorized_empty_metrics(monkeypatch):
    from api.main import require_admin_key
    app.dependency_overrides[require_admin_key] = lambda: None
    client = TestClient(app)
    try:
        monkeypatch.setattr(REGISTRY, "collect", lambda: [])

        response = client.get("/v1/admin/slo-status")
        assert response.status_code == 200
        data = response.json()

        assert data["score_availability"]["error_budget_remaining"] == 100.0
        assert data["scoring_latency"]["error_budget_remaining"] == 100.0
        assert data["webhook_delivery"]["error_budget_remaining"] == 100.0
        assert data["soroban_submission"]["error_budget_remaining"] == 100.0
    finally:
        app.dependency_overrides.clear()

def test_slo_status_calculation(monkeypatch):
    from api.main import require_admin_key
    app.dependency_overrides[require_admin_key] = lambda: None
    client = TestClient(app)

    class MockSample:
        def __init__(self, name, labels, value):
            self.name = name
            self.labels = labels
            self.value = value

    class MockMetric:
        def __init__(self, name, samples):
            self.name = name
            self.samples = samples

    avail_samples = [
        MockSample("ledgerlens_api_request_duration_seconds_count", {"method": "GET", "endpoint": "/scores/{wallet}", "status_code": "200"}, 9.0),
        MockSample("ledgerlens_api_request_duration_seconds_count", {"method": "GET", "endpoint": "/scores/{wallet}", "status_code": "500"}, 1.0),
    ]

    latency_samples = [
        MockSample("ledgerlens_scoring_latency_seconds_bucket", {"le": "2.0"}, 95.0),
        MockSample("ledgerlens_scoring_latency_seconds_bucket", {"le": "10.0"}, 100.0),
        MockSample("ledgerlens_scoring_latency_seconds_bucket", {"le": "+Inf"}, 100.0),
        MockSample("ledgerlens_scoring_latency_seconds_count", {}, 100.0),
    ]

    webhook_samples = [
        MockSample("ledgerlens_webhook_deliveries_total", {"result": "delivered"}, 48.0),
        MockSample("ledgerlens_webhook_deliveries_total", {"result": "failed"}, 2.0),
    ]

    soroban_samples = [
        MockSample("ledgerlens_soroban_submissions_total", {"status": "success"}, 9.0),
        MockSample("ledgerlens_soroban_submissions_total", {"status": "failed"}, 1.0),
        MockSample("ledgerlens_soroban_submissions_total", {"status": "skipped"}, 5.0),
    ]

    mock_metrics = [
        MockMetric("ledgerlens_api_request_duration_seconds", avail_samples),
        MockMetric("ledgerlens_scoring_latency_seconds", latency_samples),
        MockMetric("ledgerlens_webhook_deliveries_total", webhook_samples),
        MockMetric("ledgerlens_soroban_submissions_total", soroban_samples),
    ]

    try:
        monkeypatch.setattr(REGISTRY, "collect", lambda: mock_metrics)

        response = client.get("/v1/admin/slo-status")
        assert response.status_code == 200
        data = response.json()

        assert data["score_availability"]["sli"] == 90.0
        assert data["score_availability"]["error_budget_remaining"] == -1900.0

        assert data["scoring_latency"]["sli"] == 95.0
        assert data["scoring_latency"]["error_budget_remaining"] == -400.0

        assert data["webhook_delivery"]["sli"] == 96.0
        assert data["webhook_delivery"]["error_budget_remaining"] == -300.0

        assert data["soroban_submission"]["sli"] == 90.0
        assert data["soroban_submission"]["error_budget_remaining"] == -900.0
    finally:
        app.dependency_overrides.clear()
