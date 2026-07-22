import asyncio
import time

import pytest

from ingestion.horizon_streamer import (
    BoundedTradeQueue,
    HorizonStreamer,
    StreamerMetrics,
    _parse_trade,
)


def _trade(number: int):
    token = f"{number}-0"
    return _parse_trade(
        {
            "id": token,
            "paging_token": token,
            "ledger": number,
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


@pytest.mark.asyncio
async def test_bounded_queue_put_get_under_capacity():
    queue = BoundedTradeQueue(maxsize=2)

    assert await queue.put(_trade(1)) is True
    assert queue.depth() == 1
    assert (await queue.get()).id == "1-0"
    assert queue.depth() == 0
    assert queue.dropped_count() == 0
    assert queue.high_water_mark_reached_count() == 0


@pytest.mark.asyncio
async def test_drop_newest_discards_incoming_trade():
    queue = BoundedTradeQueue(maxsize=1, overflow_strategy="drop_newest")

    assert await queue.put(_trade(1)) is True
    assert await queue.put(_trade(2)) is False
    assert (await queue.get()).id == "1-0"
    assert queue.dropped_count() == 1
    assert queue.depth() == 0
    assert queue.high_water_mark_reached_count() == 2


@pytest.mark.asyncio
async def test_drop_oldest_prioritizes_recency():
    queue = BoundedTradeQueue(maxsize=1, overflow_strategy="drop_oldest")

    assert await queue.put(_trade(1)) is True
    assert await queue.put(_trade(2)) is True
    assert (await queue.get()).id == "2-0"
    assert queue.dropped_count() == 1


@pytest.mark.asyncio
async def test_block_strategy_waits_for_capacity():
    queue = BoundedTradeQueue(maxsize=1, overflow_strategy="block")
    await queue.put(_trade(1))

    pending = asyncio.create_task(queue.put(_trade(2)))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(pending), timeout=0.02)

    await queue.get()
    assert await asyncio.wait_for(pending, timeout=0.1) is True
    assert (await queue.get()).id == "2-0"


def test_queue_rejects_invalid_configuration_and_strategy_changes():
    with pytest.raises(ValueError):
        BoundedTradeQueue(maxsize=0)
    with pytest.raises(ValueError):
        BoundedTradeQueue(overflow_strategy="invalid")

    queue = BoundedTradeQueue()
    with pytest.raises(RuntimeError):
        queue.overflow_strategy = "block"

    with pytest.raises(TypeError):
        HorizonStreamer(asyncio.Queue())


@pytest.mark.asyncio
async def test_maybe_throttle_uses_exponential_delay_and_decays(tmp_path, monkeypatch):
    queue = BoundedTradeQueue(maxsize=2)
    streamer = HorizonStreamer(
        queue,
        checkpoint=_checkpoint(tmp_path),
        high_water_ratio=0.5,
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("ingestion.horizon_streamer.asyncio.sleep", fake_sleep)
    await queue.put(_trade(1))
    await streamer._maybe_throttle()
    await streamer._maybe_throttle()

    assert delays == [0.05, 0.1]
    assert streamer._throttle_level == 2

    await queue.get()
    await streamer._maybe_throttle()
    await streamer._maybe_throttle()
    assert streamer._throttle_level == 0
    metrics = streamer.metrics_snapshot()
    assert metrics.high_water_mark_hits == 2
    assert metrics.throttle_sleep_total_seconds == pytest.approx(0.15)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["drop_newest", "drop_oldest"])
async def test_fast_producer_slow_consumer_stays_bounded(
    strategy, tmp_path, monkeypatch
):
    queue = BoundedTradeQueue(maxsize=5, overflow_strategy=strategy)
    streamer = HorizonStreamer(
        queue,
        checkpoint=_checkpoint(tmp_path),
        high_water_ratio=1.0,
        rate_limit=100_000,
    )

    async def no_throttle():
        return None

    monkeypatch.setattr(streamer, "_maybe_throttle", no_throttle)
    for number in range(100):
        await streamer._enqueue(_trade(number))

    metrics = streamer.metrics_snapshot()
    assert queue.depth() == queue.maxsize
    assert queue.dropped_count() == 95
    assert metrics.events_dropped == 95
    assert metrics.queue_depth_peak == queue.maxsize
    if strategy == "drop_newest":
        assert metrics.events_queued == 5
        assert metrics.dropped_newest == 95
    else:
        assert metrics.events_queued == 100
        assert metrics.dropped_oldest == 95


@pytest.mark.asyncio
async def test_metrics_snapshot_tracks_run_paths(tmp_path, monkeypatch):
    queue = BoundedTradeQueue(maxsize=1, overflow_strategy="drop_newest")
    streamer = HorizonStreamer(
        queue,
        checkpoint=_checkpoint(tmp_path),
        high_water_ratio=1.0,
        rate_limit=100_000,
    )

    async def events():
        yield _record(1)
        yield _record(2)

    async def no_throttle():
        return None

    monkeypatch.setattr(streamer, "stream_events", events)
    monkeypatch.setattr(streamer, "_maybe_throttle", no_throttle)
    await streamer.run()

    snapshot = streamer.metrics_snapshot()
    assert isinstance(snapshot, StreamerMetrics)
    assert snapshot.events_received == 2
    assert snapshot.events_queued == 1
    assert snapshot.events_dropped == 1
    assert snapshot.queue_depth_current == 1
    assert snapshot.queue_depth_peak == 1
    assert snapshot.last_event_at is not None


def _record(number: int) -> dict:
    token = f"{number}-0"
    return {
        "id": token,
        "paging_token": token,
        "ledger": number,
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


def _checkpoint(tmp_path):
    from ingestion.checkpoint import CursorCheckpoint

    return CursorCheckpoint(tmp_path / "cursor.json")


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_drop_oldest_queue_sustains_5000_events_per_second():
    queue = BoundedTradeQueue(maxsize=1000, overflow_strategy="drop_oldest")
    trade = _trade(1)
    event_count = 10_000

    started = time.perf_counter()
    for _ in range(event_count):
        await queue.put(trade)
    elapsed = time.perf_counter() - started

    assert event_count / elapsed > 5_000
    assert queue.depth() == queue.maxsize
