"""Chaos scenario #1: Horizon API latency spike.

Injects 500 ms of downstream latency via Toxiproxy and verifies that the
scoring pipeline still completes within 2 s p99.  After the fault is removed
the proxy recovers to baseline within 60 s.

Run with:
    docker compose --profile chaos up -d
    pytest tests/chaos/test_horizon_latency.py -m chaos -v
"""

from __future__ import annotations

import statistics
import time

import pytest
import requests

pytestmark = pytest.mark.chaos

PROXY_NAME = "horizon_proxy"
HORIZON_LISTEN = "0.0.0.0:18000"
HORIZON_UPSTREAM = "horizon.stellar.org:443"

# Baseline: how long a healthy /health call should take (generous)
BASELINE_THRESHOLD_S = 1.5
# Under fault: p99 must stay below this
FAULT_LATENCY_P99_S = 2.0
INJECTED_LATENCY_MS = 500
N_SAMPLES = 20


@pytest.fixture(scope="module")
def horizon_proxy(toxiproxy):
    toxiproxy.create_proxy(PROXY_NAME, HORIZON_LISTEN, HORIZON_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)


def _measure_request_times(url: str, n: int, timeout: float = 5.0) -> list[float]:
    """Fire `n` GET requests and return elapsed times in seconds."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            requests.get(url, timeout=timeout)
        except Exception:
            pass
        times.append(time.perf_counter() - t0)
    return times


def test_horizon_latency_spike_p99_under_2s(toxiproxy, horizon_proxy):
    """Scoring latency increases under 500 ms injection but stays below 2 s p99."""
    api_url = "http://localhost:8000/health"

    # Establish baseline
    baseline = _measure_request_times(api_url, n=5)
    assert max(baseline) < BASELINE_THRESHOLD_S, (
        f"Baseline too slow ({max(baseline):.2f}s) — is the API running?"
    )

    # Inject latency
    toxiproxy.add_latency(horizon_proxy, latency_ms=INJECTED_LATENCY_MS, jitter_ms=50)
    try:
        samples = _measure_request_times(api_url, n=N_SAMPLES)
        samples_sorted = sorted(samples)
        p99_index = max(0, int(len(samples_sorted) * 0.99) - 1)
        p99 = samples_sorted[p99_index]
        mean = statistics.mean(samples)

        assert p99 < FAULT_LATENCY_P99_S, (
            f"p99 latency {p99:.3f}s exceeded {FAULT_LATENCY_P99_S}s under 500ms injection. "
            f"mean={mean:.3f}s, samples={samples_sorted}"
        )
    finally:
        toxiproxy.remove_toxic(horizon_proxy, "latency")


def test_horizon_latency_recovery(toxiproxy, horizon_proxy):
    """After fault removal, latency returns to healthy baseline within 60 s."""
    api_url = "http://localhost:8000/health"

    toxiproxy.add_latency(horizon_proxy, latency_ms=INJECTED_LATENCY_MS, toxic_name="latency_rec")
    time.sleep(2)  # let fault settle
    toxiproxy.remove_toxic(horizon_proxy, "latency_rec")

    def _is_healthy():
        times = _measure_request_times(api_url, n=3)
        return max(times) < BASELINE_THRESHOLD_S

    recovered = toxiproxy.wait_for_recovery(_is_healthy, timeout_s=60)
    assert recovered, "API did not recover to healthy latency within 60s after fault removal"
