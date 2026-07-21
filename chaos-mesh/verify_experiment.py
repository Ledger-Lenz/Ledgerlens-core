import time
import requests

HEALTH_URL = "http://localhost:8000/health"
METRICS_URL = "http://localhost:8000/metrics"


def assert_recovery(health_url: str, timeout_s: int = 60) -> None:
    """Poll GET /health until status == 'ok' or timeout_s elapses; raise on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"Health endpoint did not recover within {timeout_s}s")


def main() -> int:
    # Simple placeholder: in real usage, this script would be invoked per experiment
    try:
        assert_recovery(HEALTH_URL)
        print("✅ Health recovered")
        return 0
    except Exception as e:
        print(f"❌ Recovery failed: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
