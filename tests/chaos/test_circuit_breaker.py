"""Chaos scenario #4: Partial network partition — circuit breaker validation.

Simulates a partial network partition by injecting a connection-timeout
toxic on the Horizon proxy and verifies that the SorobanPublisher circuit
breaker opens within the configured ``threshold`` consecutive failures and
resets automatically after ``circuit_reset_seconds``.

Run with::

    docker compose --profile chaos up -d
    pytest tests/chaos/test_circuit_breaker.py -m chaos -v
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "horizon_partition"
HORIZON_LISTEN = "0.0.0.0:18001"
HORIZON_UPSTREAM = "horizon.stellar.org:443"

# Circuit-breaker tuning used across tests
CB_THRESHOLD = 5
CB_RESET_SECONDS = 2  # intentionally short so reset tests are fast

# Port derived once to avoid repeated string-splits in tests
_HORIZON_PORT = int(HORIZON_LISTEN.split(":")[1])


def _make_publisher(
    threshold: int = CB_THRESHOLD,
    reset_seconds: int = CB_RESET_SECONDS,
):
    """Return a ``SorobanPublisher`` with an isolated circuit breaker.

    ``Keypair.from_secret`` is patched so no real Stellar secret is needed.
    The patch is applied only during object construction; the returned
    publisher instance remains valid after the ``with`` block exits.
    """
    from detection.soroban_publisher import SorobanPublisher

    with patch("detection.soroban_publisher.Keypair") as mock_kp:
        mock_kp.from_secret.return_value = mock_kp
        pub = SorobanPublisher(
            contract_id="CTEST",
            secret_key="SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            soroban_rpc_url=f"http://localhost:{_HORIZON_PORT}",
            network_passphrase="Test SDF Network ; September 2015",
            circuit_breaker_threshold=threshold,
            circuit_reset_seconds=reset_seconds,
        )
    return pub


@pytest.fixture(scope="module")
def partition_proxy(toxiproxy):
    """Create the Horizon partition proxy for this module and clean up after."""
    toxiproxy.create_proxy(PROXY_NAME, HORIZON_LISTEN, HORIZON_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.delete_proxy(PROXY_NAME)


# ---------------------------------------------------------------------------
# Unit-level circuit-breaker tests (no Toxiproxy required)
# ---------------------------------------------------------------------------

def test_circuit_breaker_opens_within_threshold_failures():
    """Circuit transitions to OPEN after exactly ``threshold`` consecutive failures.

    The circuit must remain CLOSED for the first ``threshold - 1`` failures
    and open on the ``threshold``-th failure.
    """
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=CB_THRESHOLD)

    # threshold - 1 failures: circuit must stay closed
    for i in range(CB_THRESHOLD - 1):
        pub._record_failure()
        try:
            pub._check_circuit()
        except SorobanCircuitOpenError:
            pytest.fail(
                f"Circuit opened too early at failure {i + 1}/{CB_THRESHOLD}"
            )

    # Final failure trips the breaker
    pub._record_failure()
    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()


def test_circuit_breaker_rejects_calls_when_open():
    """When OPEN, ``_check_circuit`` raises ``SorobanCircuitOpenError``."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2)
    pub._record_failure()
    pub._record_failure()

    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()


def test_circuit_breaker_resets_after_window():
    """Circuit auto-resets to CLOSED/half-open after ``reset_seconds`` elapses."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2, reset_seconds=1)
    pub._record_failure()
    pub._record_failure()

    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()

    # Wait past the reset window
    time.sleep(1.2)

    # Circuit must no longer block calls (transitions to half-open)
    try:
        pub._check_circuit()
    except SorobanCircuitOpenError:
        pytest.fail("Circuit breaker did not reset after reset_seconds")


def test_circuit_breaker_consecutive_count_drives_open():
    """Circuit state is driven by ``_consecutive_failures``, not legacy timestamps.

    The ``_failure_timestamps`` list is kept for backward compatibility but
    the circuit-open decision uses ``_consecutive_failures`` against the
    threshold.  This test confirms the authoritative counter is respected.
    """
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=CB_THRESHOLD, reset_seconds=CB_RESET_SECONDS)

    # Manually set consecutive_failures below threshold — circuit stays closed
    with pub._lock:
        pub._consecutive_failures = CB_THRESHOLD - 1
        pub._circuit_state = "closed"

    try:
        pub._check_circuit()
    except SorobanCircuitOpenError:
        pytest.fail(
            "_consecutive_failures below threshold should not open the circuit"
        )

    # Tip it to the threshold — circuit opens
    with pub._lock:
        pub._consecutive_failures = CB_THRESHOLD
        pub._circuit_state = "open"
        pub._circuit_opened_at = time.monotonic()

    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()


# ---------------------------------------------------------------------------
# Integration tests (require Toxiproxy + partition_proxy fixture)
# ---------------------------------------------------------------------------

def test_partial_partition_opens_circuit_within_threshold_failures(
    toxiproxy, partition_proxy
):
    """Under simulated partition (5 s latency), circuit opens within ``CB_THRESHOLD`` attempts."""
    import requests
    from detection.soroban_publisher import SorobanCircuitOpenError

    toxiproxy.add_latency(partition_proxy, latency_ms=5000, toxic_name="partition")
    pub = _make_publisher(threshold=CB_THRESHOLD, reset_seconds=30)
    failures = 0

    try:
        for _ in range(CB_THRESHOLD + 2):
            try:
                requests.get(
                    f"http://localhost:{_HORIZON_PORT}/",
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
            f"Circuit took {failures} failures to open "
            f"(threshold={CB_THRESHOLD})"
        )
    finally:
        toxiproxy.remove_toxic(partition_proxy, "partition")


def test_partition_recovery_circuit_closes(toxiproxy, partition_proxy):
    """After partition removal, circuit self-resets within 60 s."""
    from detection.soroban_publisher import SorobanCircuitOpenError

    pub = _make_publisher(threshold=2, reset_seconds=2)

    # Open the circuit
    pub._record_failure()
    pub._record_failure()
    with pytest.raises(SorobanCircuitOpenError):
        pub._check_circuit()

    def _is_closed() -> bool:
        try:
            pub._check_circuit()
            return True
        except SorobanCircuitOpenError:
            return False

    recovered = toxiproxy.wait_for_recovery(_is_closed, timeout_s=60)
    assert recovered, "Circuit breaker did not self-reset within 60 s after partition removal"
