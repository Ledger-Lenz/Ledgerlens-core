"""Tests for ingestion.replay_buffer.OrderBookReplayBuffer."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


from ingestion.data_models import OrderBookEvent
from ingestion.replay_buffer import OrderBookReplayBuffer

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(token: str, **kwargs: Any) -> OrderBookEvent:
    defaults = dict(
        id=token,
        timestamp=_TS,
        account="GABC",
        asset_pair="XLM/USDC",
        side="sell",
        amount=100.0,
        price=1.0,
        event_type="created",
    )
    defaults.update(kwargs)
    return OrderBookEvent(**defaults)


# ---------------------------------------------------------------------------
# In-order events are drained immediately
# ---------------------------------------------------------------------------

def test_inorder_drain():
    buf = OrderBookReplayBuffer()
    buf.ingest(_event("100"))
    buf.ingest(_event("101"))
    buf.ingest(_event("102"))
    result = buf.drain()
    assert [e.id for e in result] == ["100", "101", "102"]
    assert buf.size == 0


def test_single_event_drained():
    buf = OrderBookReplayBuffer()
    buf.ingest(_event("50"))
    result = buf.drain()
    assert len(result) == 1
    assert result[0].id == "50"


# ---------------------------------------------------------------------------
# Out-of-order events held until gap filled
# ---------------------------------------------------------------------------

def test_gap_holds_later_events():
    buf = OrderBookReplayBuffer()
    # Ingest 100 first so _last_emitted_key is set, then introduce a gap.
    buf.ingest(_event("100"))
    buf.drain()  # emits 100; _last_emitted = 100

    buf.ingest(_event("102"))
    buf.ingest(_event("103"))
    result = buf.drain()
    # 101 is missing → 102 and 103 must be held back.
    assert result == []
    assert buf.size == 2


def test_gap_filled_releases_all():
    buf = OrderBookReplayBuffer()
    buf.ingest(_event("100"))
    buf.drain()

    buf.ingest(_event("102"))
    buf.ingest(_event("103"))
    assert buf.drain() == []

    # Fill the gap.
    buf.ingest(_event("101"))
    result = buf.drain()
    assert [e.id for e in result] == ["101", "102", "103"]
    assert buf.size == 0


def test_out_of_order_arrival_fills_gap():
    buf = OrderBookReplayBuffer()
    # Arrive out-of-order from the very first event.
    buf.ingest(_event("102"))
    buf.ingest(_event("101"))
    buf.ingest(_event("100"))
    result = buf.drain()
    assert [e.id for e in result] == ["100", "101", "102"]


# ---------------------------------------------------------------------------
# Gap timeout causes buffered events to be emitted
# ---------------------------------------------------------------------------

def test_gap_timeout_releases_held_events():
    buf = OrderBookReplayBuffer(gap_timeout_seconds=10.0)
    buf.ingest(_event("100"))
    buf.drain()

    buf.ingest(_event("102"))  # gap at 101
    assert buf.drain() == []

    # Simulate 15 seconds passing.
    future = buf._ingested_at["102"] + 15.0
    result = buf.drain(now_ts=future)
    assert [e.id for e in result] == ["102"]


def test_timeout_emits_in_sorted_order():
    buf = OrderBookReplayBuffer(gap_timeout_seconds=5.0)
    buf.ingest(_event("100"))
    buf.drain()

    buf.ingest(_event("103"))
    buf.ingest(_event("102"))

    future = buf._ingested_at["102"] + 10.0
    result = buf.drain(now_ts=future)
    assert [e.id for e in result] == ["102", "103"]


# ---------------------------------------------------------------------------
# flush_all returns all events sorted
# ---------------------------------------------------------------------------

def test_flush_all_sorted():
    buf = OrderBookReplayBuffer()
    for tok in ["105", "101", "103", "102", "104"]:
        buf.ingest(_event(tok))
    result = buf.flush_all()
    assert [e.id for e in result] == ["101", "102", "103", "104", "105"]
    assert buf.size == 0


def test_flush_all_empty():
    buf = OrderBookReplayBuffer()
    assert buf.flush_all() == []


# ---------------------------------------------------------------------------
# max_size overflow triggers a flush
# ---------------------------------------------------------------------------

def test_max_size_overflow_flushes():
    buf = OrderBookReplayBuffer(max_size=3, gap_timeout_seconds=60.0)
    # First emit a token so subsequent ones would normally be held for gap checks.
    buf.ingest(_event("100"))
    buf.drain()

    # These would be held (gap at 101, 102) but triggering max_size should release them.
    buf.ingest(_event("103"))
    buf.ingest(_event("104"))
    # Third ingest hits max_size=3 threshold and expires everything.
    buf.ingest(_event("105"))

    result = buf.drain()
    assert len(result) == 3
    emitted_ids = {e.id for e in result}
    assert emitted_ids == {"103", "104", "105"}


# ---------------------------------------------------------------------------
# Duplicate ingestion is ignored
# ---------------------------------------------------------------------------

def test_duplicate_ignored():
    buf = OrderBookReplayBuffer()
    buf.ingest(_event("200"))
    buf.ingest(_event("200"))
    assert buf.size == 1


# ---------------------------------------------------------------------------
# size property
# ---------------------------------------------------------------------------

def test_size_tracks_buffer():
    buf = OrderBookReplayBuffer()
    assert buf.size == 0
    buf.ingest(_event("1"))
    assert buf.size == 1
    buf.drain()
    assert buf.size == 0
