"""Soroban contract client with uncertainty-aware score submission.

Extends the existing ``SorobanPublisher`` (in ``detection.soroban_publisher``)
with a ``submit_score_with_uncertainty`` method that passes ``score_lower``
and ``score_upper`` as additional Soroban ``i128`` fields (scaled ×100 for
integer representation).

This module also provides a circuit breaker around Soroban RPC calls so that
callers (API, analyst tooling) degrade gracefully when RPC is slow or
degraded: sustained failures trip the breaker and callers receive fast,
clearly-labeled stale responses from a last-known-good cache instead of
hanging on cascading timeouts. The breaker recovers automatically once RPC
health is restored.

.. code-block:: rust
    :caption: Required ledgerlens-contract extension (PR target)

    /// Extended RiskScore struct that includes conformal prediction interval.
    /// Add to ``ledgerlens-score/src/lib.rs``.
    #[contracttype]
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct RiskScoreWithUncertainty {
        pub wallet: Address,
        pub asset_pair: Symbol,
        pub score: u32,           // 0-100
        pub score_lower: i128,    // scaled ×100
        pub score_upper: i128,    // scaled ×100
        pub timestamp: u64,
    }

    /// New contract function to submit a score with uncertainty bounds.
    /// Add alongside existing ``submit_score``.
    #[extern]
    pub fn submit_score_with_uncertainty(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        score_lower: i128,
        score_upper: i128,
        timestamp: u64,
    );

The matching PR should be opened against the
`ledgerlens-contract <https://github.com/your-org/ledgerlens-contract>`_ repo.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TypeVar

from detection.risk_score import RiskScore
from detection.soroban_publisher import SorobanPublisher

logger = logging.getLogger("ledgerlens.contract_client")

T = TypeVar("T")


class UncertaintyBoundsUnsupportedError(RuntimeError):
    """Raised when uncertainty bounds cannot actually be written on-chain.

    The contract-side ``submit_score_with_uncertainty`` (see module docstring)
    is not deployed yet. Raising is deliberate: the alternative -- quietly
    submitting a plain ``submit_score`` and logging as though the bounds were
    persisted -- makes the logs assert something about on-chain state that is
    not true.
    """


class SorobanCircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open and no cached fallback exists."""


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Configurable thresholds for the Soroban RPC circuit breaker.

    Parameters
    ----------
    failure_rate_threshold:
        Fraction of failures (0.0-1.0) within the rolling window that trips
        the breaker.
    min_samples:
        Minimum number of calls in the window before the failure rate is
        evaluated. Prevents tripping on a single unlucky call.
    window_seconds:
        Length of the rolling window used to compute the failure rate.
    open_seconds:
        How long the breaker stays open before allowing a probe (half-open).
    call_timeout_seconds:
        Per-call timeout budget. Calls exceeding this are counted as failures.
    """

    failure_rate_threshold: float = 0.5
    min_samples: int = 5
    window_seconds: float = 60.0
    open_seconds: float = 30.0
    call_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class RpcResult:
    """Response contract for RPC-backed reads.

    ``stale`` is the discriminator callers use to tell fallback data apart
    from live data. When ``stale`` is True, ``value`` came from the
    last-known-good cache and ``reason`` explains why (e.g. breaker open).
    """

    value: Any
    stale: bool
    reason: Optional[str] = None
    cached_at: Optional[float] = None


class SorobanCircuitBreaker:
    """Circuit breaker wrapping Soroban RPC calls with cache fallback.

    Tracks a rolling window of call outcomes. When the failure rate exceeds
    ``failure_rate_threshold`` (with at least ``min_samples`` observations),
    the breaker opens. While open, calls fail fast and the last-known-good
    cached value is returned, marked ``stale=True``. After ``open_seconds``
    the breaker enters half-open and lets a single probe through; a success
    closes it, a failure re-opens it.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self._lock = threading.Lock()
        self._outcomes: list[tuple[float, bool]] = []  # (timestamp, success)
        self._opened_at: Optional[float] = None
        self._half_open_probe_in_flight = False
        self._cache: Dict[str, RpcResult] = {}

    # -- state helpers -----------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        self._outcomes = [(t, ok) for (t, ok) in self._outcomes if t >= cutoff]

    def _failure_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        failures = sum(1 for _, ok in self._outcomes if not ok)
        return failures / len(self._outcomes)

    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.config.open_seconds:
                return False  # half-open: allow a probe
            return True

    def _record(self, success: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._outcomes.append((now, success))
            if success:
                # A successful probe closes the breaker and clears history.
                self._opened_at = None
                self._half_open_probe_in_flight = False
                self._outcomes = [(now, True)]
                return
            if (
                len(self._outcomes) >= self.config.min_samples
                and self._failure_rate() >= self.config.failure_rate_threshold
            ):
                self._opened_at = now
                logger.warning(
                    "Soroban circuit breaker OPEN: failure_rate=%.2f over %d samples",
                    self._failure_rate(),
                    len(self._outcomes),
                )

    # -- cache -------------------------------------------------------------

    def cache_get(self, key: str) -> Optional[RpcResult]:
        with self._lock:
            return self._cache.get(key)

    def cache_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = RpcResult(
                value=value, stale=False, cached_at=time.monotonic()
            )

    # -- main entry point --------------------------------------------------

    def call(
        self,
        key: str,
        fn: Callable[[], T],
        timeout_seconds: Optional[float] = None,
    ) -> RpcResult:
        """Invoke ``fn`` under breaker protection with cache fallback.

        Returns an ``RpcResult``. Live results have ``stale=False`` and are
        cached as last-known-good. When the breaker is open (or the call
        fails/times out), the cached value is returned with ``stale=True``.
        Raises ``SorobanCircuitOpenError`` if no cached fallback exists.
        """
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.config.call_timeout_seconds
        )

        if self.is_open():
            return self._fallback(key, reason="circuit_open")

        started = time.monotonic()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - any RPC failure counts
            self._record(success=False)
            logger.warning("Soroban RPC call failed for key=%s: %s", key, exc)
            return self._fallback(key, reason=f"rpc_error: {exc}")

        elapsed = time.monotonic() - started
        if elapsed > timeout:
            self._record(success=False)
            logger.warning(
                "Soroban RPC call timed out for key=%s (%.2fs > %.2fs)",
                key,
                elapsed,
                timeout,
            )
            return self._fallback(key, reason="rpc_timeout")

        self._record(success=True)
        self.cache_put(key, value)
        return RpcResult(value=value, stale=False)

    def _fallback(self, key: str, reason: str) -> RpcResult:
        cached = self.cache_get(key)
        if cached is None:
            raise SorobanCircuitOpenError(
                f"Soroban RPC unavailable ({reason}) and no cached state for key={key}"
            )
        logger.warning(
            "Serving STALE cached Soroban state for key=%s (reason=%s)", key, reason
        )
        return RpcResult(
            value=cached.value,
            stale=True,
            reason=reason,
            cached_at=cached.cached_at,
        )


def submit_score_with_uncertainty(
    publisher: SorobanPublisher,
    risk_score: RiskScore,
    dry_run: bool = False,
    allow_downgrade: bool = False,
) -> str | None:
    """Submit a risk score with conformal prediction uncertainty bounds.

    Parameters
    ----------
    publisher:
        Initialized ``SorobanPublisher`` instance.
    risk_score:
        ``RiskScore`` instance containing score, wallet, asset_pair,
        and optional uncertainty fields (``score_lower``, ``score_upper``).
    dry_run:
        If True, log the submission but do not send it on-chain.
    allow_downgrade:
        Accept a plain ``submit_score`` that drops ``score_lower``/
        ``score_upper`` while the contract-side function is unavailable. The
        downgrade is logged at WARNING, naming the bounds that were dropped.

    Returns
    -------
    Transaction hash on success, ``None`` on skip (``dry_run=True``).

    Raises
    ------
    SorobanSubmissionError
        On unrecoverable submission failure.
    SorobanCircuitOpenError
        When the circuit breaker is open.
    UncertaintyBoundsUnsupportedError
        When the contract cannot store uncertainty bounds and the caller has
        not opted into a downgraded submission via ``allow_downgrade``.
    """
    score_lower = risk_score.score_lower if risk_score.score_lower is not None else 0.0
    score_upper = risk_score.score_upper if risk_score.score_upper is not None else 100.0

    # Scale float bounds to i128 ×100 for integer representation
    score_lower_scaled = int(round(score_lower * 100))
    score_upper_scaled = int(round(score_upper * 100))

    if dry_run:
        logger.info(
            "[DRY-RUN] Would submit score_with_uncertainty: "
            "wallet=%s pair=%s score=%d lower=%d upper=%d",
            risk_score.wallet,
            risk_score.asset_pair,
            risk_score.score,
            score_lower_scaled,
            score_upper_scaled,
        )
        return None

    # The contract-side ``submit_score_with_uncertainty`` does not exist yet
    # (see module docstring). Until it does, this function cannot do what its
    # name says, so it refuses rather than silently downgrading to plain
    # ``submit_score`` and logging "Submitted score_with_uncertainty ...
    # lower=%d upper=%d" for bounds that never left the process.
    #
    # Callers that genuinely want the score persisted without its bounds must
    # say so with ``allow_downgrade=True``, which logs what actually happened.
    if not allow_downgrade:
        raise UncertaintyBoundsUnsupportedError(
            "submit_score_with_uncertainty requires the contract-side "
            "submit_score_with_uncertainty function, which is not yet deployed. "
            "Call publisher.submit_score() directly, or pass allow_downgrade=True "
            "to accept a submission that drops score_lower/score_upper."
        )

    tx_hash = publisher.submit_score(risk_score, dry_run=dry_run)
    logger.warning(
        "Submitted score WITHOUT uncertainty bounds (contract support missing): "
        "wallet=%s pair=%s score=%d tx_hash=%s; "
        "lower=%d upper=%d were NOT persisted on-chain",
        risk_score.wallet,
        risk_score.asset_pair,
        risk_score.score,
        tx_hash,
        score_lower_scaled,
        score_upper_scaled,
    )
    return tx_hash
