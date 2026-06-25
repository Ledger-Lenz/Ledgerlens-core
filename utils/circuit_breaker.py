"""A small, dependency-free circuit breaker for external calls.

Used to wrap calls to the Stellar Horizon API (`ingestion.horizon_streamer`)
and the Redis feature store (`detection.feature_store`) so that a sustained
outage in either trips a breaker that fast-fails subsequent calls instead of
retrying indefinitely and exhausting connection pools / threads.

State machine
-------------
CLOSED     -- normal operation. Failures are counted; reaching
              `failure_threshold` consecutive failures opens the circuit.
OPEN       -- calls are rejected immediately (`CircuitOpenError`) without
              attempting the underlying operation, until `recovery_timeout`
              seconds have elapsed.
HALF_OPEN  -- entered automatically once `recovery_timeout` has elapsed
              while OPEN. Exactly one probe call is allowed through: success
              closes the circuit, failure re-opens it (and resets the
              recovery timer).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised by `CircuitBreaker.call` (or callers checking `allow_request`)
    when a call is attempted while the circuit is OPEN."""

    def __init__(self, name: str):
        super().__init__(f"circuit breaker '{name}' is open; refusing call")
        self.name = name


class CircuitBreaker:
    """Tracks consecutive failures for one logical external dependency.

    Thread-safe: all state transitions happen under a single lock so
    concurrent callers (e.g. multiple streaming workers) see a consistent
    state.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        on_open: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.on_open = on_open
        self.on_close = on_close

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    # -- state inspection -----------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current state, applying the OPEN -> HALF_OPEN timeout transition
        as a side effect if `recovery_timeout` has elapsed."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def _maybe_transition_to_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("circuit_half_open name=%s", self.name)

    def allow_request(self) -> bool:
        """Whether a call should be attempted right now.

        OPEN circuits reject everything until the recovery timeout has
        elapsed, at which point exactly one caller will see HALF_OPEN and
        should treat its attempt as the probe.
        """
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state is not CircuitState.OPEN

    # -- outcome recording ------------------------------------------------

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state is not CircuitState.CLOSED:
                self._close()

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state is CircuitState.HALF_OPEN:
                # The probe attempt failed: re-open immediately and reset
                # the recovery timer rather than waiting for more failures.
                self._open()
            elif self._failure_count >= self.failure_threshold:
                self._open()

    def _open(self) -> None:
        was_open = self._state is CircuitState.OPEN
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        if not was_open:
            logger.warning(
                "circuit_open name=%s failure_count=%d", self.name, self._failure_count
            )
            if self.on_open:
                self.on_open()

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._failure_count = 0
        logger.info("circuit_closed name=%s", self.name)
        if self.on_close:
            self.on_close()

    # -- convenience wrapper ----------------------------------------------

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call `func(*args, **kwargs)` through the breaker.

        Raises `CircuitOpenError` without calling `func` at all if the
        circuit is OPEN. Records the outcome of every attempted call.
        """
        if not self.allow_request():
            raise CircuitOpenError(self.name)
        try:
            result = func(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result
