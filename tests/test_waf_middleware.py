import pytest
from fastapi.testclient import TestClient
from api.main import app
from config.settings import settings


@pytest.fixture(autouse=True)
def enable_waf():
    # Temporarily enable WAF for tests
    original_enabled = settings.waf_enabled
    settings.waf_enabled = True
    yield
    settings.waf_enabled = original_enabled


def test_waf_blocks_sqli_in_query_params():
    client = TestClient(app)
    # Try a request with SQLi in query params
    response = client.get("/v1/scores?wallet=' OR 1=1--")
    # Should return 400 (Bad Request)
    assert response.status_code == 400


def test_waf_blocks_xss_in_query_params():
    client = TestClient(app)
    # Try a request with XSS in query params
    response = client.get("/v1/scores?wallet=<script>alert('xss')</script>")
    # Should return 400 (Bad Request)
    assert response.status_code == 400


def test_waf_allows_benign_requests():
    client = TestClient(app)
    # Try a benign request
    response = client.get("/health")
    # Should succeed
    assert response.status_code == 200 or response.status_code == 503


def test_waf_blocks_oversized_body():
    client = TestClient(app)
    # Create a very large payload
    large_payload = {"data": "x" * (settings.waf_max_body_bytes + 1000)}
    response = client.post("/v1/feedback", json=large_payload)
    # Should return 413 (Payload Too Large)
    assert response.status_code == 413


# NOTE: api/adaptive_rate_limiter.py was removed (see docs/waf_and_rate_limiting.md
# "Adaptive Rate Limiting (removed)"). It was unreachable from any real request
# (its only caller, api.auth.require_api_key_scope, was itself dead code) and was
# independently broken (referenced undefined functions). Distributed per-key
# rate limiting is now handled by detection/rate_limiter.py; see
# tests/test_rate_limiter.py.
