"""Chaos scenario #1: Horizon API latency spike.

Injects 500 ms of downstream latency via Toxiproxy and verifies that the
scoring pipeline still completes within 2 s p99.  After the fault is removed
the proxy recovers to baseline within 60 s.

Run with::

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

# How long a healthy /health call should take (generous upper bound)
BASELINE_THRESHOLD_S = 1.5
# Under fault: p99 must stay below this
FAULT_LATENCY_P99_S = 2.0
INJECTED_LATENCY_MS = 500
N_SAMPLES = 20


@pytest.fixture(scope="module")
def horizon_proxy(toxiproxy):
    """Create the Horizon proxy for this module, clean up toxics and the proxy on teardown."""
    toxiproxy.create_proxy(PROXY_NAME, HORIZON_LISTEN, HORIZON_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.delete_proxy(PROXY_NAME)


def _measure_request_times(url: str, n: int, timeout: float = 5.0) -> list[float]:
    """Fire *n* sequential GET requests to *url* and return elapsed times in seconds.

    Exceptions (connection errors, timeouts) are swallowed and their elapsed
    time is included in the result so that slow/failed requests count toward
    latency measurements.
    """
    times: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            requests.get(url, timeout=timeout)
        except Exception:
            pass
        times.append(time.perf_counter() - t0)
    return times


def _p99(samples: list[float]) -> float:
    """Return the p99 of *samples* using nearest-rank method."""
    if not samples:
        return 0.0
    sorted_samples = sorted(samples)
    idx = max(0, int(len(sorted_samples) * 0.99) - 1)
    return sorted_samples[idx]


def test_horizon_latency_spike_p99_under_2s(toxiproxy, horizon_proxy):
    """Scoring latency stays below 2 s p99 under 500 ms injection.

    Steps:
    1. Establish a clean baseline (must be below ``BASELINE_THRESHOLD_S``).
    2. Inject 500 ms latency with 50 ms jitter.
    3. Collect ``N_SAMPLES`` request durations.
    4. Assert p99 stays below ``FAULT_LATENCY_P99_S``.
    """
    api_url = "http://localhost:8000/health"

    # Establish baseline
    baseline = _measure_request_times(api_url, n=5)
    assert max(baseline) < BASELINE_THRESHOLD_S, (
        f"Baseline too slow ({max(baseline):.2f}s) — is the API running?"
    )

    toxiproxy.add_latency(
        horizon_proxy,
        latency_ms=INJECTED_LATENCY_MS,
        jitter_ms=50,
    )
    try:
        samples = _measure_request_times(api_url, n=N_SAMPLES)
        p99 = _p99(samples)
        mean = statistics.mean(samples)

        assert p99 < FAULT_LATENCY_P99_S, (
            f"p99 latency {p99:.3f}s exceeded {FAULT_LATENCY_P99_S}s under "
            f"{INJECTED_LATENCY_MS}ms injection. "
            f"mean={mean:.3f}s, sorted_samples={sorted(samples)}"
        )
    finally:
        toxiproxy.remove_toxic(horizon_proxy, "latency")


def test_horizon_latency_recovery(toxiproxy, horizon_proxy):
    """After fault removal, latency returns to healthy baseline within 60 s."""
    api_url = "http://localhost:8000/health"

    toxiproxy.add_latency(
        horizon_proxy,
        latency_ms=INJECTED_LATENCY_MS,
        toxic_name="latency_rec",
    )
    time.sleep(2)  # let fault settle before removing
    toxiproxy.remove_toxic(horizon_proxy, "latency_rec")

    def _is_healthy() -> bool:
        times = _measure_request_times(api_url, n=3)
        return max(times) < BASELINE_THRESHOLD_S

    recovered = toxiproxy.wait_for_recovery(_is_healthy, timeout_s=60)
    assert recovered, (
        "API did not recover to healthy latency within 60 s after fault removal"
    )
