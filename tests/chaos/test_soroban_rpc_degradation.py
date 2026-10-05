"""Chaos scenario: Soroban RPC latency spikes, timeouts and connection resets.

Routes ``SorobanPublisher`` (driven through
``integrations.contract_client.submit_score_with_uncertainty``) at the
``soroban_rpc_proxy`` Toxiproxy listener and injects each fault profile.

Documented degraded-mode behaviour (see ``chaos-mesh/README.md``):

* every failed submission surfaces as ``SorobanSubmissionError`` within a
  bounded time — the caller is never left hanging on a slow RPC;
* after ``circuit_breaker_threshold`` consecutive failures the circuit opens
  and further submissions fail fast with ``SorobanCircuitOpenError``.

Run with::

    docker compose --profile chaos up -d
    pytest tests/chaos/test_soroban_rpc_degradation.py -m chaos -v
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "soroban_rpc_proxy"
SOROBAN_LISTEN = "0.0.0.0:18002"
SOROBAN_UPSTREAM = "soroban-testnet.stellar.org:443"
SOROBAN_RPC_URL = "http://localhost:18002"

# Upper bound for one failed submission (2 attempts × RPC client timeout + slack).
MAX_FAILURE_S = 90.0
# An open circuit must reject without touching the network.
MAX_FAST_FAIL_S = 0.5
CIRCUIT_THRESHOLD = 2


@pytest.fixture(scope="module")
def soroban_proxy(toxiproxy):
    toxiproxy.create_proxy(PROXY_NAME, SOROBAN_LISTEN, SOROBAN_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.enable_proxy(PROXY_NAME)


@pytest.fixture(autouse=True)
def _clean_proxy(toxiproxy, soroban_proxy):
    toxiproxy.reset_proxy(soroban_proxy)
    toxiproxy.enable_proxy(soroban_proxy)
    yield
    toxiproxy.reset_proxy(soroban_proxy)
    toxiproxy.enable_proxy(soroban_proxy)


@pytest.fixture
def publisher(monkeypatch):
    import detection.soroban_publisher as sp
    from stellar_sdk import Keypair

    # Isolate the RPC path: no lease coordination or persistence side effects.
    monkeypatch.setattr(sp, "acquire_submission_lease", lambda *a, **k: True)
    monkeypatch.setattr(sp, "save_submission", lambda *a, **k: None)
    monkeypatch.setattr(sp.SorobanPublisher, "_write_dead_letter", lambda *a, **k: None, raising=False)
    return sp.SorobanPublisher(
        contract_id="C" + "A" * 55,
        secret_key=Keypair.random().secret,
        soroban_rpc_url=SOROBAN_RPC_URL,
        network_passphrase="Test SDF Network ; September 2015",
        circuit_breaker_threshold=CIRCUIT_THRESHOLD,
        circuit_reset_seconds=300,
    )


def _score():
    from detection.risk_score import RiskScore

    return RiskScore(
        wallet="G" + "A" * 55,
        asset_pair="XLM/USDC",
        score=80,
        benford_flag=True,
        ml_flag=True,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
        score_lower=70.0,
        score_upper=90.0,
    )


def _submit(publisher):
    from integrations.contract_client import submit_score_with_uncertainty

    return submit_score_with_uncertainty(publisher, _score(), allow_downgrade=True)


def _assert_bounded_failure(publisher):
    from detection.soroban_publisher import SorobanSubmissionError

    t0 = time.monotonic()
    with pytest.raises(SorobanSubmissionError):
        _submit(publisher)
    assert time.monotonic() - t0 < MAX_FAILURE_S


def _assert_circuit_opens(publisher):
    from detection.soroban_publisher import SorobanCircuitOpenError

    for _ in range(CIRCUIT_THRESHOLD - 1):
        _assert_bounded_failure(publisher)
    assert publisher.health().circuit_state == "open"
    t0 = time.monotonic()
    with pytest.raises(SorobanCircuitOpenError):
        _submit(publisher)
    assert time.monotonic() - t0 < MAX_FAST_FAIL_S


def test_rpc_latency_spike_fails_bounded(toxiproxy, soroban_proxy, publisher):
    """3 s ± 1 s latency: submission fails within the bound, then circuit opens."""
    toxiproxy.add_latency(soroban_proxy, latency_ms=3000, jitter_ms=1000)
    _assert_bounded_failure(publisher)
    _assert_circuit_opens(publisher)


def test_rpc_timeout_fails_bounded(toxiproxy, soroban_proxy, publisher):
    """Stalled RPC connection (timeout toxic) is surfaced, never hangs."""
    toxiproxy.add_timeout(soroban_proxy, timeout_ms=2000)
    _assert_bounded_failure(publisher)
    _assert_circuit_opens(publisher)


def test_rpc_connection_reset_fails_bounded(toxiproxy, soroban_proxy, publisher):
    """Connection resets (proxy disabled) are surfaced and trip the breaker."""
    toxiproxy.disable_proxy(soroban_proxy)
    _assert_bounded_failure(publisher)
    _assert_circuit_opens(publisher)
