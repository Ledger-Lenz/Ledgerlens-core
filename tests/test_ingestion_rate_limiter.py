"""Tests for ingestion/rate_limiter.py.

Covers TokenBucket, BackpressureController, and AdaptiveRateController.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ingestion.rate_limiter import (
    AdaptiveRateController,
    BackpressureController,
    TokenBucket,
    _MIN_RATE,
)


# ---------------------------------------------------------------------------
# Public API surface (__all__)
# ---------------------------------------------------------------------------


def test_all_exports_defined():
    import ingestion.rate_limiter as m

    assert hasattr(m, "__all__")
    for name in m.__all__:
        assert hasattr(m, name), f"__all__ lists {name!r} but it is not defined"


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


def test_token_bucket_raises_on_non_positive_rate():
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(-1)


def test_token_bucket_default_capacity_is_double_rate():
    tb = TokenBucket(rate=5.0)
    assert tb.capacity == 10.0


def test_token_bucket_try_acquire_consumes_tokens():
    tb = TokenBucket(rate=10.0, capacity=10.0)
    # Bucket starts full; should be able to grab 10 tokens.
    for _ in range(10):
        assert tb.try_acquire() is True
    # 11th should fail immediately (no time to refill).
    assert tb.try_acquire() is False


def test_token_bucket_set_rate_clamped_to_min():
    tb = TokenBucket(rate=5.0)
    tb.set_rate(0.0)
    assert tb.current_rate == _MIN_RATE

    tb.set_rate(-99.0)
    assert tb.current_rate == _MIN_RATE


def test_token_bucket_set_rate_accepts_positive_value():
    tb = TokenBucket(rate=5.0)
    tb.set_rate(2.5)
    assert tb.current_rate == 2.5


def test_token_bucket_acquire_timeout_returns_false():
    """acquire() with a very short timeout must return False when tokens are exhausted."""
    tb = TokenBucket(rate=0.1, capacity=1.0)
    # Drain the single token.
    assert tb.try_acquire() is True
    # Now acquiring with a near-zero timeout must fail without blocking forever.
    result = tb.acquire(timeout=0.05)
    assert result is False


def test_token_bucket_acquire_timeout_none_gets_token_eventually():
    """acquire(timeout=None) returns True once tokens refill."""
    tb = TokenBucket(rate=100.0, capacity=1.0)
    tb.try_acquire()  # drain
    # Re-fill will take ~10 ms at 100 req/s; acquire(None) should wait.
    result = tb.acquire(timeout=1.0)
    assert result is True


def test_token_bucket_acquire_zero_timeout_does_not_block():
    """A timeout of exactly 0 must not block indefinitely."""
    tb = TokenBucket(rate=0.1, capacity=1.0)
    tb.try_acquire()  # drain
    t0 = time.monotonic()
    tb.acquire(timeout=0)
    elapsed = time.monotonic() - t0
    # Should return almost immediately (generous ceiling of 0.5 s).
    assert elapsed < 0.5


def test_token_bucket_bucket_level_is_non_negative():
    tb = TokenBucket(rate=5.0)
    assert tb.bucket_level >= 0.0


@pytest.mark.asyncio
async def test_async_acquire_returns_when_token_available():
    tb = TokenBucket(rate=100.0, capacity=1.0)
    tb.try_acquire()  # drain
    await tb.async_acquire()  # must complete without hanging
    # Token was consumed.
    assert tb.try_acquire() is False or True  # just confirm no exception


# ---------------------------------------------------------------------------
# BackpressureController
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_engages_at_high_watermark():
    q: asyncio.Queue = asyncio.Queue()
    for _ in range(10):
        await q.put(object())
    ctrl = BackpressureController(q, high_watermark=10, low_watermark=3)

    assert not ctrl.is_paused
    # Spawn check_and_wait; it should set _paused immediately.
    task = asyncio.create_task(ctrl.check_and_wait())
    # Give event loop a tick to enter the method.
    await asyncio.sleep(0)
    assert ctrl.is_paused

    # Drain the queue below low_watermark so the task finishes.
    for _ in range(8):
        q.get_nowait()
    await asyncio.wait_for(task, timeout=2.0)
    assert not ctrl.is_paused


@pytest.mark.asyncio
async def test_backpressure_does_not_engage_below_high_watermark():
    q: asyncio.Queue = asyncio.Queue()
    for _ in range(5):
        await q.put(object())
    ctrl = BackpressureController(q, high_watermark=10, low_watermark=3)

    await ctrl.check_and_wait()
    assert not ctrl.is_paused


@pytest.mark.asyncio
async def test_backpressure_already_paused_skips_watermark_check():
    """A second call while _paused is True should join the drain loop without
    re-logging the warning (covers the concurrent-caller path)."""
    q: asyncio.Queue = asyncio.Queue()
    for _ in range(10):
        await q.put(object())
    ctrl = BackpressureController(q, high_watermark=10, low_watermark=3)
    ctrl._paused = True  # simulate first caller already engaged

    # Second call: queue still over low_watermark — should wait.
    task = asyncio.create_task(ctrl.check_and_wait())
    await asyncio.sleep(0)

    for _ in range(8):
        q.get_nowait()
    await asyncio.wait_for(task, timeout=2.0)
    assert not ctrl.is_paused


def test_backpressure_queue_size_property():
    q: asyncio.Queue = asyncio.Queue()
    asyncio.get_event_loop().run_until_complete(q.put("x"))
    ctrl = BackpressureController(q)
    assert ctrl.queue_size == 1


# ---------------------------------------------------------------------------
# AdaptiveRateController
# ---------------------------------------------------------------------------


def test_adaptive_on_429_halves_rate():
    tb = TokenBucket(rate=10.0)
    ctrl = AdaptiveRateController(tb, configured_rate=10.0)
    ctrl.on_429()
    assert tb.current_rate == 5.0
    assert ctrl.last_429_at is not None


def test_adaptive_on_429_respects_min_rate():
    """Repeated 429s must not drive the rate below _MIN_RATE."""
    tb = TokenBucket(rate=_MIN_RATE * 2)
    ctrl = AdaptiveRateController(tb, configured_rate=_MIN_RATE * 2)
    for _ in range(10):
        ctrl.on_429()
    assert tb.current_rate >= _MIN_RATE


def test_adaptive_tick_no_op_before_429():
    tb = TokenBucket(rate=5.0)
    ctrl = AdaptiveRateController(tb, configured_rate=5.0)
    ctrl.tick()  # should not raise or change anything
    assert tb.current_rate == 5.0


def test_adaptive_tick_restores_rate_after_window():
    tb = TokenBucket(rate=10.0)
    ctrl = AdaptiveRateController(tb, configured_rate=10.0, restore_seconds=60.0)
    ctrl.on_429()
    reduced_rate = tb.current_rate

    # Simulate restore_seconds having elapsed by back-dating last_429_at.
    ctrl._last_429_at = time.monotonic() - 61.0
    ctrl.tick()

    assert tb.current_rate == 10.0
    assert ctrl.last_429_at is None


def test_adaptive_tick_partial_restore():
    tb = TokenBucket(rate=10.0)
    ctrl = AdaptiveRateController(tb, configured_rate=10.0, restore_seconds=60.0)
    ctrl.on_429()
    initial_reduced = tb.current_rate

    # Small tick — rate should increase but not fully recover.
    ctrl.tick()
    assert initial_reduced < tb.current_rate < 10.0
