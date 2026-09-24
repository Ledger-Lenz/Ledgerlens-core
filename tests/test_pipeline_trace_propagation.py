"""End-to-end trace continuity: ingestion -> detection -> webhook delivery (#1004).

A trade stamped with a trace ID at ingestion must carry the same ID through
scoring (webhook enqueue) and out to the subscriber in both the webhook
payload and the ``X-Trace-ID`` header.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from detection.event_bus import _serialize_event
from detection.risk_score import RiskScore
from detection.tracing import (
    TRACE_ID_HEADER,
    current_trace_id,
    ensure_trace_id,
    new_trace_id,
    use_trace_id,
)
from detection.webhook_worker import _deliver
from ingestion.checkpoint import CursorCheckpoint
from ingestion.horizon_streamer import BoundedTradeQueue, HorizonStreamer, _parse_trade


def _trade():
    return _parse_trade(
        {
            "id": "1-0",
            "paging_token": "1-0",
            "ledger": 1,
            "ledger_close_time": "2026-06-24T09:00:00Z",
            "base_account": "GA1",
            "counter_account": "GA2",
            "base_asset_code": "XLM",
            "counter_asset_code": "USDC",
            "counter_asset_issuer": "GISSUER",
            "base_amount": "1",
            "counter_amount": "2",
            "price": {"n": "2", "d": "1"},
            "base_is_seller": True,
        }
    )


def _score(wallet: str = "GA1") -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair="XLM/USDC:GISSUER",
        score=90,
        benford_flag=True,
        ml_flag=True,
        confidence=90,
        timestamp=datetime(2026, 6, 24, 9, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def _stub_delivery_deps():
    span = MagicMock()
    span.__enter__ = MagicMock(return_value=span)
    span.__exit__ = MagicMock(return_value=False)
    tracer = MagicMock()
    tracer.start_as_current_span.return_value = span
    counter = MagicMock()
    with patch("config.telemetry.get_tracer", return_value=tracer), \
         patch("api.metrics.webhook_deliveries_total", counter), \
         patch("detection.webhook_worker.mark_delivered"):
        yield


def test_use_trace_id_scopes_and_restores_context():
    outer = current_trace_id()
    tid = new_trace_id()
    with use_trace_id(tid):
        assert current_trace_id() == tid
        assert ensure_trace_id() == tid
    assert current_trace_id() == outer
    assert ensure_trace_id("not-a-trace-id") != "not-a-trace-id"


async def test_trace_id_continuous_from_ingestion_to_webhook_delivery(tmp_path, _stub_delivery_deps):
    # Stage 1 — ingestion: the streamer stamps the trade with a trace ID on enqueue.
    queue = BoundedTradeQueue(maxsize=10)
    streamer = HorizonStreamer(queue, checkpoint=CursorCheckpoint(tmp_path / "cursor.json"))
    assert await streamer._enqueue(_trade())
    trade = await queue.get()
    trace_id = trade.trace_id
    assert trace_id and len(trace_id) == 32
    assert "trace_id" not in trade.model_dump()  # persisted trade shape unchanged

    # Stage 2 — detection: scoring resumes the trade's trace; webhook alerts
    # and event bus envelopes are stamped with it.
    import run_pipeline

    enqueued: list[dict] = []
    subscriber = SimpleNamespace(subscriber_id="sub-1")
    with use_trace_id(trade.trace_id), \
         patch("detection.webhook_queue.init_db"), \
         patch("detection.webhook_registry.init_db"), \
         patch("detection.webhook_registry.get_matching_subscribers", return_value=[subscriber]), \
         patch("detection.webhook_queue.enqueue", side_effect=lambda sid, payload: enqueued.append(payload)):
        run_pipeline._enqueue_webhook_alerts([_score()])
        envelope = json.loads(_serialize_event(_score()))
    assert [p["trace_id"] for p in enqueued] == [trace_id]
    assert envelope["trace_id"] == trace_id

    # Stage 3 — delivery: the webhook body and headers expose the same trace ID.
    response = MagicMock(status_code=200)
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    delivery = SimpleNamespace(
        id=1,
        subscriber_id="sub-1",
        attempt_count=0,
        payload_json=json.dumps(enqueued[0], default=str),
    )
    assert await _deliver(client, delivery, SimpleNamespace(url="https://example.test/hook", secret="s"))

    kwargs = client.post.await_args.kwargs
    assert json.loads(kwargs["content"])["trace_id"] == trace_id
    assert kwargs["headers"][TRACE_ID_HEADER] == trace_id


def test_event_envelope_omits_trace_id_outside_a_trace():
    with patch("detection.event_bus.current_trace_id", return_value=None):
        assert "trace_id" not in json.loads(_serialize_event(_score()))
