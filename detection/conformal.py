"""Conformal Prediction for distribution-free uncertainty quantification.

Implements split conformal prediction (Angelopoulos & Bates, 2023) for
the LedgerLens risk score ensemble. Provides valid, finite-sample
prediction intervals at a user-specified coverage level (default 90%).
"""

import hashlib
import json
import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.conformal")


class CalibrationIntegrityError(Exception):
    """Raised when a calibration artifact's SHA-256 does not match its content."""


class ConformalCalibrator:
    """Split conformal prediction calibrator for binary classifiers.

    Computes nonconformity scores (1 - softmax score for the true class)
    on a held-out calibration set and stores the (1 - alpha)-quantile
    threshold ``q_hat`` for use at inference time.

    Parameters
    ----------
    q_hat:
        Pre-computed nonconformity threshold. Set via ``calibrate()``.
    alpha:
        Nominal miscoverage level (default 0.10 → 90 % coverage).
    """

    def __init__(self, q_hat: float | None = None, alpha: float = 0.10):
        self.q_hat = q_hat
        self.alpha = alpha
        self.n_cal: int = 0
        self._content_hash: str = ""

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def calibrate(
        self,
        model: Any,
        X_cal: pd.DataFrame,
        y_cal: pd.Series,
        alpha: float | None = None,
    ) -> "ConformalCalibrator":
        """Compute ``q_hat`` from nonconformity scores on the calibration set.

        Nonconformity score for each calibration example is
        ``1 - softmax_probability[true_class]``.

        Args:
            model:
                A trained classifier with a ``predict_proba`` method.
            X_cal:
                Calibration feature DataFrame (columns must match training).
            y_cal:
                Calibration labels.
            alpha:
                Miscoverage level (defaults to ``self.alpha``).

        Returns:
            Self for chaining.
        """
        if alpha is not None:
            self.alpha = alpha

        y_cal = y_cal.reset_index(drop=True)
        probs = model.predict_proba(X_cal)

        # Nonconformity scores: 1 - softmax score for the true class
        idx = np.arange(len(y_cal))
        n_scores = 1.0 - probs[idx, y_cal.values]

        n = len(n_scores)
        self.n_cal = n
        # Finite-sample correction: (1-alpha) quantile with rounding up
        q_level = np.ceil((n + 1) * (1.0 - self.alpha)) / n
        self.q_hat = float(np.quantile(n_scores, min(q_level, 1.0), method="higher"))

        empirical_coverage = float(np.mean(n_scores <= self.q_hat))
        logger.info(
            "Calibration: alpha=%.2f q_hat=%.4f n_cal=%d coverage=%.4f",
            self.alpha,
            self.q_hat,
            self.n_cal,
            empirical_coverage,
        )
        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_set(self, model: Any, X: pd.DataFrame) -> list[dict]:
        """Return prediction sets for each row in ``X``.

        Each result dict contains:
          - ``score``: softmax probability for class 1
          - ``prediction_set``: list of class indices included in the set
          - ``coverage_guarantee``: target coverage level (1 - alpha)
          - ``q_hat``: the nonconformity threshold used
        """
        if self.q_hat is None:
            raise RuntimeError("Call calibrate() before predict_set().")

        probs = model.predict_proba(X)
        results = []
        for row_probs in probs:
            prediction_set = [int(j) for j, p in enumerate(row_probs) if (1.0 - p) <= self.q_hat]
            results.append({
                "score": float(row_probs[1]),
                "prediction_set": prediction_set,
                "coverage_guarantee": 1.0 - self.alpha,
                "q_hat": self.q_hat,
            })
        return results

    def predict_with_interval(self, model: Any, X: pd.DataFrame) -> list[dict]:
        """Return prediction intervals for the risk score (0-100) framing.

        Applies the conformal ``q_hat`` to the softmax probability for class 1
        and maps the resulting interval to the 0-100 risk score range.

        Each result dict contains:
          - ``score``: predicted probability for class 1
          - ``lower``: lower bound of the prediction interval (0-100 scale)
          - ``upper``: upper bound of the prediction interval (0-100 scale)
        """
        if self.q_hat is None:
            raise RuntimeError("Call calibrate() before predict_with_interval().")

        probs = model.predict_proba(X)
        results = []
        for row_probs in probs:
            prob = float(row_probs[1])
            lower = max(0.0, prob - self.q_hat) * 100.0
            upper = min(1.0, prob + self.q_hat) * 100.0
            results.append({
                "score": prob,
                "lower": lower,
                "upper": upper,
            })
        return results

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Persist calibration artifact to a human-readable JSON file.

        Includes a SHA-256 digest of the serialized content for integrity
        verification on load.
        """
        if self.q_hat is None:
            raise RuntimeError("Call calibrate() before save().")

        data = {
            "q_hat": self.q_hat,
            "alpha": self.alpha,
            "n_cal": self.n_cal,
            "version": 1,
        }
        content = json.dumps(data, sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        payload = {
            "data": data,
            "sha256": digest,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        self._content_hash = digest
        logger.info("Saved calibration artifact to %s (sha256=%s)", path, digest[:16])

    @classmethod
    def load(cls, path: str) -> "ConformalCalibrator":
        """Load calibration artifact from a JSON file.

        Raises:
            CalibrationIntegrityError: if SHA-256 digest does not match.
            FileNotFoundError: if the file does not exist.
        """
        with open(path, "r") as f:
            payload = json.load(f)

        data = payload.get("data", {})
        stored_digest = payload.get("sha256", "")

        expected_content = json.dumps(data, sort_keys=True)
        actual_digest = hashlib.sha256(expected_content.encode()).hexdigest()
        if stored_digest and actual_digest != stored_digest:
            raise CalibrationIntegrityError(
                f"SHA-256 mismatch: expected {stored_digest}, got {actual_digest}"
            )

        calibrator = cls(q_hat=data["q_hat"], alpha=data["alpha"])
        calibrator.n_cal = data.get("n_cal", 0)
        calibrator._content_hash = stored_digest
        logger.info(
            "Loaded calibration artifact from %s (sha256=%s)", path, stored_digest[:16]
        )
        return calibrator

    @property
    def content_hash(self) -> str:
        """SHA-256 hex digest of the last saved / loaded artifact."""
        return self._content_hash
