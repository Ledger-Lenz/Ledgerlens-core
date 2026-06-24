"""Tests for ingestion.rate_limiter and horizon_streamer backpressure/adaptive rate."""

import asyncio
import time
from unittest.mock import patch

import pytest

from ingestion.rate_limiter import TokenBucket
from ingestion.horizon_streamer import (
    AdaptiveRateController,
    BackpressureController,
    HorizonStreamer,
)


class TestTokenBucket:
    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(rate=0)
        with pytest.raises(ValueError):
            TokenBucket(rate=-1)

    def test_full_bucket_acquire(self):
        bucket = TokenBucket(rate=10, capacity=10)
        assert bucket.try_acquire() is True

    def test_empty_bucket_fails(self):
        bucket = TokenBucket(rate=10, capacity=1)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False

    def test_refill_over_time(self):
        bucket = TokenBucket(rate=100, capacity=100)
        for _ in range(100):
            bucket.try_acquire()
        assert bucket.try_acquire() is False

        with patch("ingestion.rate_limiter.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 1.0
            bucket._last_refill = time.monotonic() - 1.0
            bucket._tokens = 0.0
            with bucket._lock:
                bucket._refill()
            assert bucket._tokens >= 90.0

    def test_acquire_blocking_returns_true(self):
        bucket = TokenBucket(rate=100, capacity=100)
        assert bucket.acquire(timeout=1.0) is True

    def test_acquire_timeout(self):
        bucket = TokenBucket(rate=0.1, capacity=1)
        bucket.try_acquire()
        assert bucket.acquire(timeout=0.05) is False

    def test_set_rate(self):
        bucket = TokenBucket(rate=50)
        bucket.set_rate(25)
        assert bucket.current_rate == 25

    def test_set_rate_floor(self):
        bucket = TokenBucket(rate=50)
        bucket.set_rate(0.01)
        assert bucket.current_rate == 0.1

    def test_bucket_level(self):
        bucket = TokenBucket(rate=10, capacity=10)
        level = bucket.bucket_level
        assert level > 0

    @pytest.mark.asyncio
    async def test_async_acquire(self):
        bucket = TokenBucket(rate=100, capacity=100)
        await bucket.async_acquire()


class TestBackpressureController:
    @pytest.mark.asyncio
    async def test_engages_at_high_watermark(self):
        queue = asyncio.Queue()
        for i in range(1001):
            queue.put_nowait(i)
        bp = BackpressureController(queue, high_watermark=1000, low_watermark=500)

        async def drain():
            await asyncio.sleep(0.05)
            while queue.qsize() > 499:
                queue.get_nowait()

        task = asyncio.create_task(drain())
        await bp.check_and_wait()
        await task
        assert bp.is_paused is False

    @pytest.mark.asyncio
    async def test_does_not_engage_below_watermark(self):
        queue = asyncio.Queue()
        for i in range(500):
            queue.put_nowait(i)
        bp = BackpressureController(queue, high_watermark=1000, low_watermark=500)
        await bp.check_and_wait()
        assert bp.is_paused is False

    @pytest.mark.asyncio
    async def test_paused_property(self):
        queue = asyncio.Queue()
        bp = BackpressureController(queue, high_watermark=10, low_watermark=5)
        assert bp.is_paused is False


class TestAdaptiveRateController:
    def test_on_429_halves_rate(self):
        bucket = TokenBucket(rate=50)
        ctrl = AdaptiveRateController(bucket, configured_rate=50)
        ctrl.on_429()
        assert abs(bucket.current_rate - 25.0) < 1.0

    def test_rate_floor_after_repeated_429(self):
        bucket = TokenBucket(rate=50)
        ctrl = AdaptiveRateController(bucket, configured_rate=50)
        for _ in range(20):
            ctrl.on_429()
        assert bucket.current_rate >= 0.1

    def test_tick_restores_rate(self):
        bucket = TokenBucket(rate=50)
        ctrl = AdaptiveRateController(bucket, configured_rate=50, restore_seconds=60)
        ctrl.on_429()
        halved = bucket.current_rate
        for _ in range(30):
            ctrl.tick()
        assert bucket.current_rate > halved

    def test_full_restoration(self):
        bucket = TokenBucket(rate=50)
        ctrl = AdaptiveRateController(bucket, configured_rate=50, restore_seconds=0.01)
        ctrl.on_429()
        import time as t
        t.sleep(0.02)
        ctrl.tick()
        assert abs(bucket.current_rate - 50.0) < 0.5

    def test_tick_noop_without_429(self):
        bucket = TokenBucket(rate=50)
        ctrl = AdaptiveRateController(bucket, configured_rate=50)
        ctrl.tick()
        assert bucket.current_rate == 50.0


class TestHorizonStreamer:
    def test_rate_limiter_status(self):
        streamer = HorizonStreamer(rate_limit=50)
        status = streamer.rate_limiter_status()
        assert status["configured_rate"] == 50.0
        assert status["current_rate"] == 50.0
        assert status["backpressure_active"] is False
        assert status["queue_size"] == 0
        assert status["last_429_at"] is None

    @pytest.mark.asyncio
    async def test_backpressure_blocks_enqueue(self):
        queue = asyncio.Queue()
        for i in range(1001):
            queue.put_nowait(i)
        streamer = HorizonStreamer(queue=queue, rate_limit=100, high_watermark=1000, low_watermark=500)
        assert streamer.backpressure.is_paused is False

        async def drain_later():
            await asyncio.sleep(0.05)
            while queue.qsize() > 499:
                queue.get_nowait()

        task = asyncio.create_task(drain_later())
        await streamer.backpressure.check_and_wait()
        await task
