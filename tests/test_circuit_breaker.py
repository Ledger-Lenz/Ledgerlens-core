"""Tests for utils.circuit_breaker.CircuitBreaker."""

import time

import pytest

from utils.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def test_starts_closed():
    cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=60)
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_opens_after_failure_threshold():
    cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=60)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED, "should not open before reaching the threshold"

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_success_resets_failure_count():
    cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=60)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED, "the success should have reset the streak"


def test_transitions_to_half_open_after_timeout():
    cb = CircuitBreaker("svc", failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request() is True


def test_half_open_success_closes_circuit():
    cb = CircuitBreaker("svc", failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens_circuit_and_resets_timer():
    cb = CircuitBreaker("svc", failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    # Timer should have restarted: not yet half-open again immediately.
    assert cb.allow_request() is False


def test_call_wrapper_raises_circuit_open_error_when_open():
    cb = CircuitBreaker("svc", failure_threshold=1, recovery_timeout=60)

    def boom():
        raise ValueError("downstream failure")

    with pytest.raises(ValueError):
        cb.call(boom)
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        cb.call(lambda: 1)


def test_call_wrapper_passes_through_successful_result():
    cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=60)
    assert cb.call(lambda x, y: x + y, 2, y=3) == 5
    assert cb.state == CircuitState.CLOSED


def test_on_open_and_on_close_callbacks_fire_once_per_transition():
    opens = []
    closes = []
    cb = CircuitBreaker(
        "svc",
        failure_threshold=1,
        recovery_timeout=0.05,
        on_open=lambda: opens.append(1),
        on_close=lambda: closes.append(1),
    )
    cb.record_failure()
    cb.record_failure()  # still open; on_open should not fire again
    assert opens == [1]

    time.sleep(0.06)
    cb.record_success()
    assert closes == [1]
