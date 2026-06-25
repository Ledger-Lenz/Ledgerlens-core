"""Conformal Prediction for distribution-free uncertainty quantification.

Implements split conformal prediction (Angelopoulos & Bates, 2023) for
the LedgerLens risk score ensemble. Provides valid, finite-sample
prediction intervals at a user-specified coverage level (default 90%).

Multi-class extension (Issue-109): Implements RAPS (Regularised Adaptive
Prediction Sets, Angelopoulos et al. 2021) for the three-class risk taxonomy
{clean=0, suspicious=1, wash=2}. RAPS nonconformity scores reduce prediction
set sizes via a regularisation term while maintaining marginal coverage.

Coverage guarantee (marginal, finite-sample): P(Y ∈ C(X)) ≥ 1 − α.
"""

import hashlib
import json
import logging
from typing import Any, List

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.conformal")

# ---------------------------------------------------------------------------
# Three-class taxonomy
# ---------------------------------------------------------------------------

CLASS_LABELS = {0: "clean", 1: "suspicious", 2: "wash"}
CLASS_BOUNDARIES = {
    0: (0, 33),    # score 0–33  → clean
    1: (34, 66),   # score 34–66 → suspicious
    2: (67, 100),  # score 67–100 → wash
}


def score_to_class(score: int) -> int:
    """Map a 0-100 risk score to its three-class label index."""
    if score <= 33:
        return 0
    if score <= 66:
        return 1
    return 2


# ---------------------------------------------------------------------------
# RAPS nonconformity score
# ---------------------------------------------------------------------------


def raps_score(
    softmax_probs: np.ndarray,
    true_class: int,
    lambda_reg: float = 0.2,
    k_reg: int = 2,
) -> float:
    """Compute the RAPS nonconformity score for `true_class`.

    s(x, y) = Σ_{j: π_j ≥ π_y} π_j  +  λ · max(o(y) − k_reg, 0)

    where o(y) is the 1-indexed rank of class y in the softmax sorted
    in descending probability order.

    Args:
        softmax_probs: 1-D array of class probabilities (must sum to ~1).
        true_class: Index of the ground-truth class (0-indexed).
        lambda_reg: RAPS regularisation weight (default 0.2).
        k_reg: Rank below which regularisation is zero (default 2).

    Returns:
        Scalar RAPS nonconformity score.
    """
    sorted_idx = np.argsort(-softmax_probs)  # descending order
    rank = int(np.where(sorted_idx == true_class)[0][0])  # 0-indexed
    cumsum = np.cumsum(softmax_probs[sorted_idx])
    score = cumsum[rank] + lambda_reg * max(rank + 1 - k_reg, 0)
    return float(score)


# ---------------------------------------------------------------------------
# Multi-class calibration and prediction
# ---------------------------------------------------------------------------


def calibrate_raps(
    cal_softmax_probs: np.ndarray,
    cal_labels: np.ndarray,
    alpha: float = 0.10,
    lambda_reg: float = 0.2,
    k_reg: int = 2,
) -> float:
    """Compute the RAPS calibration threshold q_hat.

    Returns the (1 − α)(1 + 1/n)-quantile of RAPS nonconformity scores
    on the calibration set.

    Args:
        cal_softmax_probs: Array of shape (n, K) with softmax probabilities.
        cal_labels: Integer class labels of shape (n,).
        alpha: Miscoverage level (default 0.10 → 90 % coverage).
        lambda_reg: RAPS regularisation weight (default 0.2).
        k_reg: RAPS k_reg parameter (default 2).

    Returns:
        q_hat: calibration threshold (float).
    """
    n = len(cal_labels)
    scores = np.array([
        raps_score(cal_softmax_probs[i], int(cal_labels[i]), lambda_reg, k_reg)
        for i in range(n)
    ])
    q_level = np.ceil((1 - alpha) * (1 + 1 / n)) / (1 + 1 / n)
    q_level = min(q_level, 1.0)  # clamp to valid quantile range
    q_hat = float(np.quantile(scores, q_level, method="higher"))
    return q_hat


def predict_set_raps(
    softmax_probs: np.ndarray,
    q_hat: float,
    lambda_reg: float = 0.2,
    k_reg: int = 2,
) -> List[int]:
    """Return the RAPS prediction set for one softmax probability vector.

    Includes class k when raps_score(softmax_probs, k) ≤ q_hat.

    Args:
        softmax_probs: 1-D array of class probabilities.
        q_hat: Calibration threshold from :func:`calibrate_raps`.
        lambda_reg: RAPS regularisation weight (default 0.2).
        k_reg: RAPS k_reg parameter (default 2).

    Returns:
        Sorted list of class indices in the prediction set.
    """
    prediction_set = [
        k for k in range(len(softmax_probs))
        if raps_score(softmax_probs, k, lambda_reg, k_reg) <= q_hat
    ]
    return sorted(prediction_set)


def validate_coverage(
    cal_probs: np.ndarray,
    cal_labels: np.ndarray,
    q_hat: float,
    alpha: float = 0.10,
    tolerance: float = 0.02,
    lambda_reg: float = 0.2,
    k_reg: int = 2,
) -> float:
    """Assert that empirical coverage on the calibration set is within tolerance.

    Args:
        cal_probs: Calibration softmax probabilities (n, K).
        cal_labels: True class labels (n,).
        q_hat: RAPS threshold from :func:`calibrate_raps`.
        alpha: Target miscoverage level.
        tolerance: Maximum allowed deviation from target coverage.
        lambda_reg: RAPS regularisation weight.
        k_reg: RAPS k_reg parameter.

    Returns:
        Empirical coverage fraction.

    Raises:
        AssertionError: if achieved coverage deviates more than `tolerance`
            from the target `1 − alpha`.
    """
    n = len(cal_labels)
    covered = sum(
        int(cal_labels[i]) in predict_set_raps(cal_probs[i], q_hat, lambda_reg, k_reg)
        for i in range(n)
    )
    achieved = covered / n
    target = 1.0 - alpha
    assert abs(achieved - target) <= tolerance, (
        f"Coverage {achieved:.3f} deviates from target {target:.3f} by more than {tolerance}"
    )
    return achieved


# ---------------------------------------------------------------------------
# RAPSConformal class
# ---------------------------------------------------------------------------


class RAPSConformal:
    """RAPS conformal predictor for multi-class risk classification.

    Produces prediction sets that contain the true class label with
    marginal coverage ≥ 1 − alpha (e.g. 90 % for alpha = 0.10).

    Parameters
    ----------
    alpha:
        Miscoverage level (default 0.10 → 90 % coverage).
    lambda_reg:
        RAPS regularisation weight λ (default 0.2).
    k_reg:
        Rank threshold below which no regularisation is applied (default 2).
    """

    def __init__(
        self,
        alpha: float = 0.10,
        lambda_reg: float = 0.2,
        k_reg: int = 2,
    ) -> None:
        self.alpha = alpha
        self.lambda_reg = lambda_reg
        self.k_reg = k_reg
        self.q_hat: float | None = None
        self.n_calibration: int = 0
        self.achieved_coverage: float | None = None

    def calibrate(
        self,
        cal_softmax_probs: np.ndarray,
        cal_labels: np.ndarray,
    ) -> "RAPSConformal":
        """Compute q_hat from calibration data.

        Args:
            cal_softmax_probs: Shape (n, K) softmax probabilities.
            cal_labels: Integer class labels of shape (n,).

        Returns:
            Self for chaining.
        """
        n = len(cal_labels)
        if n < 100:
            logger.warning(
                "Calibration set has only %d samples (< 100); q_hat will be unreliable", n
            )
        self.n_calibration = n
        self.q_hat = calibrate_raps(
            cal_softmax_probs, cal_labels, self.alpha, self.lambda_reg, self.k_reg
        )
        if not np.isfinite(self.q_hat) or self.q_hat <= 0:
            logger.warning(
                "q_hat is not a finite positive float (%s); predictions will include all classes",
                self.q_hat,
            )
        try:
            self.achieved_coverage = validate_coverage(
                cal_softmax_probs, cal_labels, self.q_hat, self.alpha,
                tolerance=0.05,  # relaxed tolerance for small cal sets
                lambda_reg=self.lambda_reg, k_reg=self.k_reg,
            )
        except AssertionError:
            # Log but do not raise — training must not crash on coverage warning
            self.achieved_coverage = None
            logger.warning("Coverage validation failed; q_hat may be unreliable")
        logger.info(
            "RAPS calibration: alpha=%.2f q_hat=%.4f n_cal=%d coverage=%s",
            self.alpha, self.q_hat, self.n_calibration,
            f"{self.achieved_coverage:.4f}" if self.achieved_coverage is not None else "unknown",
        )
        return self

    def predict_set(
        self,
        test_softmax_probs: np.ndarray,
        alpha: float | None = None,
    ) -> List[int]:
        """Return the RAPS prediction set for one softmax probability vector.

        Args:
            test_softmax_probs: 1-D softmax probability array of length K.
            alpha: Override miscoverage level (uses self.alpha if None).

        Returns:
            Sorted list of class indices in the prediction set.
        """
        if self.q_hat is None:
            raise RuntimeError("Call calibrate() before predict_set().")
        effective_alpha = alpha if alpha is not None else self.alpha
        if effective_alpha != self.alpha:
            # Cannot reuse q_hat calibrated for a different alpha
            logger.warning(
                "predict_set called with alpha=%.2f but calibrated with alpha=%.2f; "
                "q_hat may give incorrect coverage",
                effective_alpha, self.alpha,
            )
        q_hat = self.q_hat if np.isfinite(self.q_hat) else np.inf
        return predict_set_raps(test_softmax_probs, q_hat, self.lambda_reg, self.k_reg)

    def save(self, path: str) -> None:
        """Persist RAPS calibration artifact to JSON with SHA-256 integrity."""
        if self.q_hat is None:
            raise RuntimeError("Call calibrate() before save().")
        data = {
            "q_hat": self.q_hat,
            "alpha": self.alpha,
            "lambda_reg": self.lambda_reg,
            "k_reg": self.k_reg,
            "n_calibration": self.n_calibration,
            "achieved_coverage": self.achieved_coverage,
            "version": 1,
            "type": "raps_multiclass",
        }
        content = json.dumps(data, sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        payload = {"data": data, "sha256": digest}
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("Saved RAPS calibration artifact to %s (sha256=%s)", path, digest[:16])

    @classmethod
    def load(cls, path: str) -> "RAPSConformal":
        """Load a RAPS calibration artifact from JSON.

        Raises:
            CalibrationIntegrityError: if SHA-256 digest does not match.
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
        instance = cls(
            alpha=data["alpha"],
            lambda_reg=data.get("lambda_reg", 0.2),
            k_reg=data.get("k_reg", 2),
        )
        instance.q_hat = data["q_hat"]
        instance.n_calibration = data.get("n_calibration", 0)
        instance.achieved_coverage = data.get("achieved_coverage")

        if not np.isfinite(instance.q_hat) or instance.q_hat <= 0:
            logger.warning(
                "Loaded q_hat=%s is not a finite positive float; "
                "predictions will include all classes",
                instance.q_hat,
            )
        logger.info("Loaded RAPS calibration artifact from %s", path)
        return instance


class CalibrationIntegrityError(Exception):
    """Raised when a calibration artifact's SHA-256 does not match its content."""


def aggregate_ensemble_softmax(
    rf_proba: np.ndarray,
    xgb_proba: np.ndarray,
    lgbm_proba: np.ndarray,
) -> np.ndarray:
    """Average three binary model outputs into a 3-class softmax vector.

    For models that output binary [p_negative, p_positive], maps p_positive to
    the appropriate 3-class bucket via :func:`score_to_class` and redistributes
    probability mass across the three classes.

    Returns a (3,) array with probabilities summing to 1.
    """
    def _to_3class(p_positive: float) -> np.ndarray:
        c = score_to_class(round(p_positive * 100))
        v = np.zeros(3)
        v[c] = 1.0
        return v

    rf_3 = _to_3class(float(rf_proba[1]) if len(rf_proba) == 2 else float(rf_proba[2]))
    xgb_3 = _to_3class(float(xgb_proba[1]) if len(xgb_proba) == 2 else float(xgb_proba[2]))
    lgbm_3 = _to_3class(float(lgbm_proba[1]) if len(lgbm_proba) == 2 else float(lgbm_proba[2]))
    raw = np.mean([rf_3, xgb_3, lgbm_3], axis=0)
    total = raw.sum()
    if total > 0:
        return raw / total
    return np.ones(3) / 3.0


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

    def calibrate_multiclass(
        self,
        cal_softmax_probs: np.ndarray,
        cal_labels: np.ndarray,
        alpha: float = 0.10,
    ) -> "RAPSConformal":
        """Calibrate a :class:`RAPSConformal` predictor on 3-class ensemble output.

        Takes the three-class softmax probabilities averaged across all base
        models (RF, XGBoost, LightGBM) and produces a calibrated
        :class:`RAPSConformal` instance.

        Args:
            cal_softmax_probs: Shape (n, 3) array of averaged softmax probabilities.
            cal_labels: Integer labels in {0, 1, 2} of shape (n,).
            alpha: Miscoverage level (default 0.10).

        Returns:
            Calibrated :class:`RAPSConformal` instance.
        """
        raps = RAPSConformal(alpha=alpha)
        raps.calibrate(cal_softmax_probs, cal_labels)
        return raps

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
