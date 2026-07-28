"""Shared fixtures for the chaos engineering test suite.

Prerequisites:
  docker compose --profile chaos up -d toxiproxy
  make test-chaos

All tests in this suite are marked with ``pytest.mark.chaos`` and are
excluded from standard CI — they run only in the dedicated chaos job.

Session-skip behaviour
----------------------
The ``require_toxiproxy`` fixture is ``autouse=True`` at session scope.
If Toxiproxy is not reachable when the session starts, *every* chaos test
is skipped via ``pytest.skip()``.  This avoids confusing collection errors
when the Docker compose profile has not been started.
"""

from __future__ import annotations

import time

import pytest
import requests

TOXIPROXY_API = "http://localhost:8474"
TOXIPROXY_HORIZON_PROXY = "horizon_proxy"
TOXIPROXY_REDIS_PROXY = "redis_proxy"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "chaos: chaos-engineering resilience tests (requires Toxiproxy)",
    )


def _toxiproxy_available() -> bool:
    """Return True when the Toxiproxy REST API responds with HTTP 200."""
    try:
        r = requests.get(f"{TOXIPROXY_API}/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_toxiproxy() -> None:
    """Skip the entire chaos suite if Toxiproxy is not reachable.

    Start Toxiproxy with::

        docker compose --profile chaos up -d toxiproxy
    """
    if not _toxiproxy_available():
        pytest.skip(
            "Toxiproxy not available — start with: "
            "docker compose --profile chaos up -d toxiproxy"
        )


@pytest.fixture(scope="session")
def toxiproxy() -> ToxiproxyClient:
    """Return a session-scoped Toxiproxy API helper."""
    return ToxiproxyClient(TOXIPROXY_API)


class ToxiproxyClient:
    """Thin wrapper around the Toxiproxy REST API."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Proxy lifecycle
    # ------------------------------------------------------------------

    def create_proxy(self, name: str, listen: str, upstream: str) -> dict:
        """Create a proxy, or return the existing one if it already exists."""
        payload = {
            "name": name,
            "listen": listen,
            "upstream": upstream,
            "enabled": True,
        }
        r = self.session.post(f"{self.base}/proxies", json=payload, timeout=5)
        if r.status_code == 409:
            return self.session.get(
                f"{self.base}/proxies/{name}", timeout=5
            ).json()
        r.raise_for_status()
        return r.json()

    def delete_proxy(self, name: str) -> None:
        """Delete a proxy by name; silently ignores 404."""
        self.session.delete(f"{self.base}/proxies/{name}", timeout=5)

    def reset_proxy(self, name: str) -> None:
        """Remove all toxics from a proxy (restore clean pass-through)."""
        r = self.session.get(f"{self.base}/proxies/{name}/toxics", timeout=5)
        if r.status_code != 200:
            return
        for toxic in r.json():
            self.session.delete(
                f"{self.base}/proxies/{name}/toxics/{toxic['name']}",
                timeout=5,
            )

    def enable_proxy(self, name: str) -> None:
        """Re-enable a previously disabled proxy."""
        self.session.post(
            f"{self.base}/proxies/{name}", json={"enabled": True}, timeout=5
        )

    def disable_proxy(self, name: str) -> None:
        """Disable a proxy to simulate a connection-refused condition."""
        self.session.post(
            f"{self.base}/proxies/{name}", json={"enabled": False}, timeout=5
        )

    # ------------------------------------------------------------------
    # Toxics
    # ------------------------------------------------------------------

    def add_latency(
        self,
        proxy: str,
        latency_ms: int,
        jitter_ms: int = 0,
        toxic_name: str = "latency",
    ) -> dict:
        """Add a downstream latency toxic to *proxy*."""
        payload = {
            "name": toxic_name,
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": jitter_ms},
        }
        r = self.session.post(
            f"{self.base}/proxies/{proxy}/toxics", json=payload, timeout=5
        )
        r.raise_for_status()
        return r.json()

    def remove_toxic(self, proxy: str, toxic_name: str) -> None:
        """Remove a named toxic; silently ignores 404."""
        self.session.delete(
            f"{self.base}/proxies/{proxy}/toxics/{toxic_name}", timeout=5
        )

    def add_timeout(
        self,
        proxy: str,
        timeout_ms: int,
        toxic_name: str = "timeout",
    ) -> dict:
        """Simulate a connection timeout (TCP reset after *timeout_ms* ms)."""
        payload = {
            "name": toxic_name,
            "type": "timeout",
            "stream": "upstream",
            "toxicity": 1.0,
            "attributes": {"timeout": timeout_ms},
        }
        r = self.session.post(
            f"{self.base}/proxies/{proxy}/toxics", json=payload, timeout=5
        )
        r.raise_for_status()
        return r.json()

    def wait_for_recovery(
        self,
        check_fn,
        timeout_s: int = 60,
        interval_s: float = 2.0,
    ) -> bool:
        """Poll *check_fn()* until it returns ``True`` or *timeout_s* elapses.

        Any exception raised by *check_fn* is caught and treated as a
        non-recovery result so the poller keeps retrying.

        Returns ``True`` if *check_fn* returned ``True`` within the
        deadline, ``False`` otherwise.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if check_fn():
                    return True
            except Exception:
                pass
            time.sleep(interval_s)
        return False
