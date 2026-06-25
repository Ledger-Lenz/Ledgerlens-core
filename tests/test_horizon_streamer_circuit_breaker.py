"""Simulates a sustained Horizon outage and verifies the circuit breaker
wrapping `ingestion.horizon_streamer.stream_trades_with_cursor` opens,
fast-fails, and recovers correctly.
"""

import pytest

import ingestion.horizon_streamer as horizon_streamer
from ingestion.horizon_streamer import stream_trades_with_cursor
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


class _FakeEvent:
    def __init__(self, data, event_id="cursor-1"):
        self.data = data
        self.id = event_id


class _AlwaysFailsSSEClient:
    """Raises as soon as it's iterated -- simulates Horizon being unreachable."""

    def __init__(self, url, headers=None):
        self.url = url

    def __iter__(self):
        raise ConnectionError("Horizon unreachable")


class _OneTradeThenStopsSSEClient:
    """Yields exactly one well-formed trade event, then the stream ends."""

    _TRADE_JSON = (
        '{"id": "123", "ledger_close_time": "2026-01-01T00:00:00Z", '
        '"base_account": "GA1", "counter_account": "GA2", '
        '"base_asset_code": "XLM", "base_asset_issuer": null, '
        '"counter_asset_code": "USDC", '
        '"counter_asset_issuer": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN", '
        '"base_amount": "100.0", "counter_amount": "500.0", '
        '"price": {"n": "5", "d": "1"}, "base_is_seller": true}'
    )

    def __init__(self, url, headers=None):
        self.url = url

    def __iter__(self):
        yield _FakeEvent(self._TRADE_JSON, event_id="cursor-2")


@pytest.fixture(autouse=True)
def _fresh_circuit(monkeypatch):
    """Each test gets its own breaker instance so state never leaks across tests."""
    fresh = CircuitBreaker(name="horizon", failure_threshold=3, recovery_timeout=0.05)
    monkeypatch.setattr(horizon_streamer, "horizon_circuit", fresh)
    return fresh


def test_opens_after_threshold_consecutive_connection_failures(monkeypatch, _fresh_circuit):
    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _AlwaysFailsSSEClient)
    monkeypatch.setattr(horizon_streamer, "_RECONNECT_BACKOFF_SECONDS", 0.0)

    with pytest.raises(CircuitOpenError):
        list(stream_trades_with_cursor(cursor="now"))

    assert _fresh_circuit.state == CircuitState.OPEN


def test_stops_attempting_connections_once_open(monkeypatch, _fresh_circuit):
    """Once OPEN, a brand-new call must fail immediately without ever
    constructing an SSEClient (no further connection attempts)."""
    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _AlwaysFailsSSEClient)
    monkeypatch.setattr(horizon_streamer, "_RECONNECT_BACKOFF_SECONDS", 0.0)
    with pytest.raises(CircuitOpenError):
        list(stream_trades_with_cursor(cursor="now"))

    call_count = {"n": 0}

    class _CountingSSEClient(_AlwaysFailsSSEClient):
        def __init__(self, url, headers=None):
            call_count["n"] += 1
            super().__init__(url, headers)

    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _CountingSSEClient)

    with pytest.raises(CircuitOpenError):
        list(stream_trades_with_cursor(cursor="now"))
    assert call_count["n"] == 0, "should not attempt a connection while circuit is open"


def test_recovers_via_half_open_probe_after_timeout(monkeypatch, _fresh_circuit):
    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _AlwaysFailsSSEClient)
    monkeypatch.setattr(horizon_streamer, "_RECONNECT_BACKOFF_SECONDS", 0.0)
    with pytest.raises(CircuitOpenError):
        list(stream_trades_with_cursor(cursor="now"))
    assert _fresh_circuit.state == CircuitState.OPEN

    import time

    time.sleep(0.06)
    assert _fresh_circuit.state == CircuitState.HALF_OPEN

    # Horizon has recovered: the probe connection now succeeds.
    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _OneTradeThenStopsSSEClient)
    trades = list(stream_trades_with_cursor(cursor="now"))
    assert len(trades) == 1
    assert _fresh_circuit.state == CircuitState.CLOSED


def test_successful_stream_never_opens_circuit(monkeypatch, _fresh_circuit):
    monkeypatch.setattr(horizon_streamer.sseclient, "SSEClient", _OneTradeThenStopsSSEClient)
    trades = list(stream_trades_with_cursor(cursor="now"))
    assert len(trades) == 1
    assert _fresh_circuit.state == CircuitState.CLOSED
