"""Backpressure / slow-client load tests for the gRPC scoring stream (#971).

The servicer generator is driven directly: not pulling from it is exactly what
gRPC's send thread does while a slow client's transport window is full.  (A
real stalled channel cannot be simulated reliably because the C-core client
keeps growing its receive window via BDP probing even when the app stops
reading.)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import grpc
import pytest

import api.grpc_scoring_service as svc
from api.metrics import grpc_backpressure_disconnects_total
from config.settings import settings
from detection.risk_score import RiskScore
from generated import scoring_pb2

BUFFER = 8
TIMEOUT = 0.3
TOTAL = 100_000


class _Abort(Exception):
    pass


def _context():
    ctx = MagicMock()
    ctx.invocation_metadata.return_value = [("x-ledgerlens-admin-key", "admin-key")]
    ctx.peer.return_value = "ipv4:127.0.0.1:1"

    def abort(code, details):
        ctx.aborted_with = code
        raise _Abort(details)

    ctx.abort.side_effect = abort
    return ctx


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", "admin-key")
    monkeypatch.setattr(settings, "grpc_stream_buffer_size", BUFFER)
    monkeypatch.setattr(settings, "grpc_slow_client_timeout_seconds", TIMEOUT)
    monkeypatch.setattr(settings, "grpc_max_batch_wallets", TOTAL + 1)


@pytest.fixture
def produced(monkeypatch):
    counter = {"n": 0}
    score = RiskScore(
        wallet="GLOADTEST", asset_pair="XLM/USDC", score=50, benford_flag=False,
        ml_flag=False, confidence=80, timestamp=datetime.now(timezone.utc),
    )

    def fake_lookup(wallet, asset_pair=None):
        counter["n"] += 1
        return [score]

    monkeypatch.setattr(svc.storage, "get_latest_scores", fake_lookup)
    return counter


def _requests(n=TOTAL):
    return (scoring_pb2.ScoreRequest(wallet=f"G{i}") for i in range(n))


def test_slow_consumer_memory_bounded_and_disconnected(produced):
    ctx = _context()
    before = grpc_backpressure_disconnects_total._value.get()
    stream = svc.ScoringServicer().BatchScoreWallets(_requests(), ctx)

    next(stream)  # read one message, then stall like a stuck client
    time.sleep(TIMEOUT * 3)

    # Producer stopped once the bounded buffer filled: memory is O(BUFFER), not O(TOTAL).
    assert produced["n"] <= BUFFER + 3
    ctx.cancel.assert_called_once()
    assert grpc_backpressure_disconnects_total._value.get() == before + 1

    with pytest.raises(_Abort):
        list(stream)
    assert ctx.aborted_with == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_consumer_slower_than_producer_but_within_timeout_is_served(produced):
    ctx = _context()
    stream = svc.ScoringServicer().BatchScoreWallets(_requests(200), ctx)
    received = 0
    for _ in stream:
        received += 1
        if received % 50 == 0:
            time.sleep(TIMEOUT / 3)  # periodic stalls shorter than the timeout
    assert received == 200
    ctx.cancel.assert_not_called()


def test_batch_limit_still_enforced(produced, monkeypatch):
    monkeypatch.setattr(settings, "grpc_max_batch_wallets", 5)
    ctx = _context()
    with pytest.raises(_Abort, match="maximum limit of 5"):
        list(svc.ScoringServicer().BatchScoreWallets(_requests(6), ctx))
    assert ctx.aborted_with == grpc.StatusCode.RESOURCE_EXHAUSTED

