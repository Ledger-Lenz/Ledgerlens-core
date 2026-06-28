"""Tests for graceful shutdown behaviour in api/main.py."""



class TestHealthReady:
    def test_ready_returns_200_when_running(self):
        import api.main as api_main
        from fastapi.testclient import TestClient

        original = api_main._shutting_down
        try:
            api_main._shutting_down = False
            client = TestClient(api_main.app, raise_server_exceptions=False)
            resp = client.get("/health/ready")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ready"
        finally:
            api_main._shutting_down = original

    def test_ready_returns_503_when_shutting_down(self):
        import api.main as api_main
        from fastapi.testclient import TestClient

        original = api_main._shutting_down
        try:
            api_main._shutting_down = True
            client = TestClient(api_main.app, raise_server_exceptions=False)
            resp = client.get("/health/ready")
            assert resp.status_code == 503
            assert resp.json()["status"] == "shutting_down"
        finally:
            api_main._shutting_down = original


class TestShutdownGuardMiddleware:
    def test_rejects_requests_during_shutdown(self):
        import api.main as api_main
        from fastapi.testclient import TestClient

        original = api_main._shutting_down
        try:
            api_main._shutting_down = True
            client = TestClient(api_main.app, raise_server_exceptions=False)
            resp = client.get("/v1/scores")
            assert resp.status_code == 503
            assert "shutting down" in resp.json()["detail"].lower()
        finally:
            api_main._shutting_down = original

    def test_allows_health_during_shutdown(self):
        import api.main as api_main
        from fastapi.testclient import TestClient

        original = api_main._shutting_down
        try:
            api_main._shutting_down = True
            client = TestClient(api_main.app, raise_server_exceptions=False)
            resp = client.get("/health/ready")
            assert resp.status_code == 503
            assert resp.json()["status"] == "shutting_down"
        finally:
            api_main._shutting_down = original


class TestShutdownTimeout:
    def test_default_timeout(self):
        import api.main as api_main
        assert isinstance(api_main.SHUTDOWN_TIMEOUT, int)
        assert api_main.SHUTDOWN_TIMEOUT > 0
