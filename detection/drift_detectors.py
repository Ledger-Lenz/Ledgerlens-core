"""Streaming, low-latency drift detectors for the real-time scoring path.

Implements two classical online change-point detectors — ADWIN (Bifet &
Gavaldà, "Learning from Time-Changing Data with Adaptive Windowing", SDM
2007) and the Page-Hinkley test (Page, "Continuous Inspection Schemes",
Biometrika 1954) — operating on individual feature values as they are
computed during real-time scoring (``detection/model_inference.py``).

This is deliberately a *different* mechanism from the batch PSI-histogram
comparison in ``detection/drift_monitor.py``: PSI compares a full training
reference distribution against a recent batch, triggered manually or by
`cli.py retrain-check` — useful for slow, gradual drift characterisation,
but with detection latency bounded only by operator/cron cadence. The
detectors here are per-observation, streaming, and bounded-memory, designed
to catch sudden mean/variance shifts within ~100-200 observations (see
``.github/ISSUES/ISSUE-109.md``). The two mechanisms are complementary, not
competing: PSI for thorough offline characterisation, these for fast
online alarms that gate conformal recalibration (see
:meth:`detection.conformal.ConformalCalibrator.adapt_online`).

Implementation notes
---------------------
Both detectors are implemented from scratch (no ``river``/``skmultiflow``
dependency) to avoid adding a new heavy dependency for this feature alone.

ADWIN here uses the standard "exponential histogram of buckets" engineering
approximation used by MOA/river: raw values are folded into buckets that are
merged once a row holds more than ``max_buckets_per_row`` entries, bounding
memory to O(M log(W/M)) per feature regardless of stream length. Cut-points
are checked at bucket boundaries (not every single sample) using the
variance-aware Hoeffding-style bound from the paper's Theorem 2. This trades
a small amount of split precision for O(log W) amortised cost per update.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger("ledgerlens.drift_detectors")

# ---------------------------------------------------------------------------
# Sensitivity configuration (Issue-109: ADWIN_DELTA / PAGE_HINKLEY_THRESHOLD)
# ---------------------------------------------------------------------------

ADWIN_DELTA = float(os.environ.get("ADWIN_DELTA", "0.002"))
PAGE_HINKLEY_THRESHOLD = float(os.environ.get("PAGE_HINKLEY_THRESHOLD", "50.0"))
PAGE_HINKLEY_DELTA = float(os.environ.get("PAGE_HINKLEY_DELTA", "0.005"))

# How many observations a fired detector stays "active" for, gating whether
# conformal adaptation reacts to feedback (see conformal.adapt_online).
DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS = int(os.environ.get("DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS", "200"))


def _combine_stats(n1: int, total1: float, var1: float, n2: int, total2: float, var2: float):
    """Pairwise (count, sum, sum-of-squared-deviations) combination formula.

    Standard parallel-variance-combination (Chan et al., 1979); used both to
    merge two ADWIN buckets and to fold a bucket list into aggregate stats,
    without ever re-touching the raw observations.
    """
    n = n1 + n2
    if n == 0:
        return 0, 0.0, 0.0
    mean1 = total1 / n1 if n1 else 0.0
    mean2 = total2 / n2 if n2 else 0.0
    total = total1 + total2
    delta = mean1 - mean2
    variance = var1 + var2 + (delta * delta) * n1 * n2 / n
    return n, total, variance


class _Bucket:
    __slots__ = ("n", "total", "variance")

    def __init__(self, n: int, total: float, variance: float):
        self.n = n
        self.total = total
        self.variance = variance


class ADWINDriftDetector:
    """ADaptive WINdowing streaming change-point detector.

    Maintains a variable-length window of the most recent observations for
    one feature; bounded-memory via an exponential histogram of buckets.
    ``update(value)`` returns True the moment a statistically significant
    change-point is found, at which point the older sub-window is dropped.

    Parameters
    ----------
    delta:
        Confidence parameter (smaller = fewer false positives, slower to
        react). Default 0.002 per ``ADWIN_DELTA``.
    max_buckets_per_row:
        Bucket-list compression factor M; memory is O(M log(W/M)).
    """

    def __init__(self, delta: float = ADWIN_DELTA, max_buckets_per_row: int = 5):
        if not 0.0 < delta < 1.0:
            raise ValueError("delta must be in (0, 1)")
        self.delta = delta
        self.max_buckets_per_row = max_buckets_per_row
        self._rows: list[deque] = [deque()]
        self.width: int = 0
        self.total: float = 0.0
        self.variance: float = 0.0
        self.drift_detected: bool = False
        self.n_detections: int = 0
        self.last_detection_at: Optional[int] = None

    @property
    def estimation(self) -> float:
        """Current window mean (best estimate of the live feature value)."""
        return self.total / self.width if self.width else 0.0

    def update(self, value: float) -> bool:
        """Feed one observation; returns True iff a change-point fired."""
        self._rows[0].append(_Bucket(1, float(value), 0.0))
        self._compress()
        self._refold()

        self.drift_detected = False
        while self._maybe_cut():
            self.drift_detected = True
            self._refold()

        if self.drift_detected:
            self.n_detections += 1
            self.last_detection_at = self.width
        return self.drift_detected

    def _compress(self) -> None:
        row = 0
        while row < len(self._rows) and len(self._rows[row]) > self.max_buckets_per_row:
            if row + 1 >= len(self._rows):
                self._rows.append(deque())
            b1 = self._rows[row].popleft()
            b2 = self._rows[row].popleft()
            n, total, var = _combine_stats(b1.n, b1.total, b1.variance, b2.n, b2.total, b2.variance)
            self._rows[row + 1].append(_Bucket(n, total, var))
            row += 1

    def _ordered_buckets(self) -> list:
        """Return all buckets oldest -> newest.

        Within a row, front = oldest (buckets are appended at the back and
        merged from the front). Across rows, higher row index holds strictly
        older data (a row's buckets are only created by merging the
        currently-oldest pair from the row below), so iterating rows from
        the highest index down yields a chronological ordering.
        """
        ordered = []
        for row in reversed(self._rows):
            ordered.extend(row)
        return ordered

    def _refold(self) -> None:
        width, total, variance = 0, 0.0, 0.0
        for b in self._ordered_buckets():
            width, total, variance = _combine_stats(width, total, variance, b.n, b.total, b.variance)
        self.width, self.total, self.variance = width, total, variance

    def _epsilon_cut(self, n0: int, n1: int) -> float:
        n = self.width
        if n <= 1 or n0 < 1 or n1 < 1:
            return float("inf")
        delta_prime = self.delta / max(np.log(max(n, 2)), 1e-9)
        m = 1.0 / (1.0 / n0 + 1.0 / n1)  # harmonic-mean effective sample size
        variance = max(self.variance / n, 1e-12)
        term1 = float(np.sqrt((2.0 / m) * variance * np.log(2.0 / delta_prime)))
        term2 = (2.0 / (3.0 * m)) * np.log(2.0 / delta_prime)
        return term1 + term2

    def _maybe_cut(self) -> bool:
        if self.width < 2 * self.max_buckets_per_row:
            return False
        ordered = self._ordered_buckets()
        n0, total0, var0 = 0, 0.0, 0.0
        for i in range(len(ordered) - 1):
            b = ordered[i]
            n0, total0, var0 = _combine_stats(n0, total0, var0, b.n, b.total, b.variance)
            n1 = self.width - n0
            if n0 < 1 or n1 < 1:
                continue
            total1 = self.total - total0
            mean0 = total0 / n0
            mean1 = total1 / n1
            epsilon = self._epsilon_cut(n0, n1)
            if abs(mean0 - mean1) > epsilon:
                self._drop_oldest(i + 1)
                return True
        return False

    def _drop_oldest(self, count: int) -> None:
        remaining = count
        for row in reversed(self._rows):
            while remaining > 0 and row:
                row.popleft()
                remaining -= 1
            if remaining == 0:
                break


class PageHinkleyDetector:
    """Page-Hinkley test for detecting a sustained mean shift in a stream.

    Standard streaming/ML adaptation (as used by river/MOA): track a
    cumulative sum of deviations from the running mean (offset by a
    tolerance ``delta``), and its running minimum; fire when the gap
    between the two exceeds ``threshold``. Resets its cumulative statistics
    on firing so it can keep detecting subsequent shifts.

    Parameters
    ----------
    delta:
        Magnitude of change tolerated before it counts toward the
        cumulative sum (noise tolerance). Default 0.005.
    threshold:
        ``lambda`` in the original paper; higher = fewer false alarms,
        slower detection. Default 50.0 per ``PAGE_HINKLEY_THRESHOLD``.
    alpha:
        Forgetting factor for the running mean (1.0 = no forgetting).
    """

    def __init__(self, delta: float = PAGE_HINKLEY_DELTA, threshold: float = PAGE_HINKLEY_THRESHOLD, alpha: float = 1.0):
        self.delta = delta
        self.threshold = threshold
        self.alpha = alpha
        self._mean = 0.0
        self._n = 0
        self._cumulative = 0.0
        self._min_cumulative = 0.0
        self.drift_detected: bool = False
        self.n_detections: int = 0
        self.last_detection_at: Optional[int] = None

    @property
    def statistic(self) -> float:
        return self._cumulative - self._min_cumulative

    @property
    def n_observations(self) -> int:
        return self._n

    def update(self, value: float) -> bool:
        self._n += 1
        self._mean += (value - self._mean) / self._n
        self._cumulative = self.alpha * self._cumulative + (value - self._mean - self.delta)
        self._min_cumulative = min(self._min_cumulative, self._cumulative)

        self.drift_detected = self.statistic > self.threshold
        if self.drift_detected:
            self.n_detections += 1
            self.last_detection_at = self._n
            self._cumulative = 0.0
            self._min_cumulative = 0.0
        return self.drift_detected


class DriftDetectorRegistry:
    """Owns one ADWIN + one Page-Hinkley detector per scored feature.

    Bounded memory overall: O(n_features * M * log(W/M)). Designed to be
    updated once per real-time scoring call (see
    ``detection.model_inference.score_with_uncertainty``) and queried by the
    ``GET /health/drift`` endpoint and by the conformal-recalibration gate.
    """

    def __init__(self, feature_names, adwin_delta: float = ADWIN_DELTA,
                 ph_threshold: float = PAGE_HINKLEY_THRESHOLD, ph_delta: float = PAGE_HINKLEY_DELTA):
        self._feature_names = list(feature_names)
        self._adwin = {f: ADWINDriftDetector(delta=adwin_delta) for f in self._feature_names}
        self._ph = {f: PageHinkleyDetector(delta=ph_delta, threshold=ph_threshold) for f in self._feature_names}
        self.last_drifted_features: list[str] = []
        self.last_event_at: Optional[str] = None
        self._last_detection_width: Optional[int] = None

    def observe(self, feature_vector: dict) -> list[dict]:
        """Update every known feature's detectors with one observation.

        Returns a list of ``{feature, detector, magnitude}`` dicts for
        detectors that fired on this observation (empty if none fired).
        Silently ignores keys that aren't recognised features or aren't
        numeric, so it is safe to pass a raw feature vector as-is.
        """
        fired = []
        for name, value in feature_vector.items():
            adwin = self._adwin.get(name)
            ph = self._ph.get(name)
            if adwin is None or ph is None or not isinstance(value, (int, float)):
                continue
            v = float(value)
            if np.isnan(v):
                continue
            if adwin.update(v):
                fired.append({"feature": name, "detector": "adwin", "magnitude": adwin.estimation})
            if ph.update(v):
                fired.append({"feature": name, "detector": "page_hinkley", "magnitude": ph.statistic})

        if fired:
            self.last_drifted_features = sorted({e["feature"] for e in fired})
            self.last_event_at = datetime.now(timezone.utc).isoformat()
            self._last_detection_width = max(self._adwin[f].width for f in self.last_drifted_features)
            self._emit_drift_event(fired)
        return fired

    def is_active(self, cooldown_observations: int = DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS) -> bool:
        """True if a detector fired within the last ``cooldown_observations``.

        Gates conformal recalibration: adaptation only reacts to feedback
        while drift is believed to be recent, avoiding needless alpha churn
        under stationary conditions (see ``conformal.adapt_online``).
        """
        if self._last_detection_width is None:
            return False
        current_width = max((d.width for d in self._adwin.values()), default=0)
        return (current_width - self._last_detection_width) <= cooldown_observations

    def _emit_drift_event(self, fired: list[dict]) -> None:
        """Best-effort ``drift.detected`` webhook fan-out; never raises."""
        try:
            from detection.webhook_queue import enqueue
            from detection.webhook_registry import list_subscribers

            subscribers = list_subscribers(active_only=True)
            if not subscribers:
                return
            payload = {
                "event": "drift.detected",
                "features": fired,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for sub in subscribers:
                enqueue(sub.subscriber_id, payload)
        except Exception:
            logger.exception("Failed to enqueue drift.detected webhook")

    def state(self) -> dict:
        """JSON-serialisable snapshot of every detector's state, for `/health/drift`."""
        features = {}
        for name in self._feature_names:
            adwin = self._adwin[name]
            ph = self._ph[name]
            features[name] = {
                "adwin": {
                    "width": adwin.width,
                    "estimation": adwin.estimation,
                    "n_detections": adwin.n_detections,
                    "last_detection_at_width": adwin.last_detection_at,
                },
                "page_hinkley": {
                    "statistic": ph.statistic,
                    "n_observations": ph.n_observations,
                    "n_detections": ph.n_detections,
                    "last_detection_at_n": ph.last_detection_at,
                },
            }
        return {
            "drift_active": self.is_active(),
            "last_drifted_features": self.last_drifted_features,
            "last_event_at": self.last_event_at,
            "config": {
                "adwin_delta": ADWIN_DELTA,
                "page_hinkley_threshold": PAGE_HINKLEY_THRESHOLD,
                "page_hinkley_delta": PAGE_HINKLEY_DELTA,
                "cooldown_observations": DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS,
            },
            "features": features,
        }


_registry: Optional[DriftDetectorRegistry] = None


def get_drift_registry() -> DriftDetectorRegistry:
    """Process-wide singleton registry, one detector pair per known feature."""
    global _registry
    if _registry is None:
        from detection.feature_engineering import FEATURE_NAMES

        _registry = DriftDetectorRegistry(FEATURE_NAMES)
    return _registry
