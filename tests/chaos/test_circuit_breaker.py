"""Chaos scenario #4: Partial network partition — circuit breaker validation.

Simulates a partial network partition by injecting a connection timeout toxic
on the Horizon proxy and verifies that the SorobanPublisher circuit breaker
opens within 5 consecutive failures and resets automatically after the
configured window.

Run with:
    docker compose --profile chaos up -d
    pytest tests/chaos/test_circuit_breaker.py -m chaos -v
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "horizon_partition"
HORIZON_LISTEN = "0.0.0.0:18001"
HORIZON_UPSTREAM = "horizon.stellar.org:443"

CB_THRESHOLD = 5
CB_WINDOW = 60
CB_RESET_SECONDS = 2  # short for tests


def _make_publisher(threshold: int = CB_THRESHOLD, reset_seconds: int = CB_RESET_SECONDS):
    """Return a SorobanPublisher with an isolated circuit breaker for testing."""
    from unittest.mock import patch
    from detection.soroban_publisher import SorobanPublisher

    # Patch Keypair so we don't need a real Stellar secret
    with patch("detection.soroban_publisher.Keypair") as mock_kp:
        mock_kp.from_secret.return_value = mock_kp
        pub = SorobanPublisher(
            contract_id="CTEST",
            secret_key="SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            soroban_rpc_url="http://localhost:18001",
            network_passphrase="Test SDF Network ; September 2015",
            circuit_breaker_threshold=threshold,
            circuit_breaker_window=CB_WINDOW,
            circuit_reset_seconds=reset_seconds,
        )
    return pub


@pytest.fixture(scope="module")
def partition_proxy(toxiproxy):
    toxiproxy.create_proxy(PROXY_NAME, HORIZON_LISTEN, HORIZON_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.delete_proxy(PROXY_NAME)


def test_circuit_breaker_opens_within_5_failures():
    """Circuit breaker transitions to OPEN after exactly `threshold` failures."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=CB_THRESHOLD)

    # Record threshold - 1 failures: circuit must stay closed
    for i in range(CB_THRESHOLD - 1):
        pub._record_failure()
        # Should not raise yet
        try:
            pub._check_circuit()
        except SorobanCircuitOpenError:
            pytest.fail(f"Circuit opened too early at failure {i + 1}/{CB_THRESHOLD}")

    # One more failure opens it
    pub._record_failure()
    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()


def test_circuit_breaker_rejects_calls_when_open():
    """When OPEN, _check_circuit raises SorobanCircuitOpenError."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2)
    pub._record_failure()
    pub._record_failure()

    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()


def test_circuit_breaker_resets_after_window():
    """Circuit breaker auto-resets after circuit_reset_seconds elapses."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2, reset_seconds=1)
    pub._record_failure()
    pub._record_failure()

    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()

    # Wait past reset window
    time.sleep(1.2)

    # Should no longer raise after reset
    try:
        pub._check_circuit()
    except SorobanCircuitOpenError:
        pytest.fail("Circuit breaker did not reset after reset_seconds")


def test_circuit_breaker_failure_timestamps_expire_with_window():
    """Failures older than circuit_breaker_window do not count toward threshold."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=CB_THRESHOLD, reset_seconds=CB_RESET_SECONDS)

    # Manually inject old timestamps (outside the 60s window)
    old_time = time.time() - CB_WINDOW - 1
    with pub._lock:
        pub._failure_timestamps = [old_time] * (CB_THRESHOLD + 1)

    # Old failures should be pruned — circuit should be CLOSED
    try:
        pub._check_circuit()
    except SorobanCircuitOpenError:
        pytest.fail("Expired failures should not keep the circuit open")


def test_partial_partition_opens_circuit_within_5_failures(toxiproxy, partition_proxy):
    """Under simulated partition (5000ms latency), circuit opens within 5 call attempts."""
    import requests
    from detection.soroban_publisher import SorobanCircuitOpenError

    toxiproxy.add_latency(partition_proxy, latency_ms=5000, toxic_name="partition")
    pub = _make_publisher(threshold=CB_THRESHOLD, reset_seconds=30)
    failures = 0

    try:
        for _ in range(CB_THRESHOLD + 2):
            try:
                requests.get(
                    f"http://localhost:{HORIZON_LISTEN.split(':')[1]}/",
                    timeout=0.2,
                )
            except Exception:
                pub._record_failure()
                failures += 1

            try:
                pub._check_circuit()
            except SorobanCircuitOpenError:
                break

        with pytest.raises(SorobanCircuitOpenError):
            pub._check_circuit()

        assert failures <= CB_THRESHOLD, (
            f"Circuit took {failures} failures to open (threshold={CB_THRESHOLD})"
        )
    finally:
        toxiproxy.remove_toxic(partition_proxy, "partition")


def test_partition_recovery_circuit_closes(toxiproxy, partition_proxy):
    """After partition removal, circuit breaker self-resets within 60 s."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2, reset_seconds=2)

    # Open the circuit
    pub._record_failure()
    pub._record_failure()
    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()

    def _is_closed():
        try:
            pub._check_circuit()
            return True
        except SorobanCircuitOpenError:
            return False

    recovered = toxiproxy.wait_for_recovery(_is_closed, timeout_s=60)
    assert recovered, "Circuit breaker did not self-reset within 60 s after partition removal"
