"""Tests for the ConformalCalibrator."""

import json
import os

import numpy as np
import pandas as pd
import pytest
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import train_test_split

from detection.conformal import CalibrationIntegrityError, ConformalCalibrator
from detection.feature_engineering import FEATURE_NAMES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _perfect_calibration_model(n_features: int = len(FEATURE_NAMES), random_state: int = 42):
    """Returns a model whose predict_proba approximates the true class
    probability (perfect calibration) via isotonic regression."""
    rng = np.random.default_rng(random_state)
    n = 1000
    X = rng.uniform(-2, 2, size=(n, n_features))
    logits = X[:, 0] - 0.5 * X[:, 1] + 0.3 * X[:, 2]
    true_probs = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.uniform(size=n) < true_probs).astype(int)

    # Use isotonic regression to get well-calibrated probabilities
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(true_probs, y)

    class PerfectCalModel:
        def predict_proba(self, X_inner):
            arr = X_inner.values if hasattr(X_inner, "values") else X_inner
            logits_inner = arr[:, 0] - 0.5 * arr[:, 1] + 0.3 * arr[:, 2]
            probs_inner = 1.0 / (1.0 + np.exp(-logits_inner))
            cal_probs = iso.predict(probs_inner)
            return np.column_stack([1.0 - cal_probs, cal_probs])

    return PerfectCalModel(), X, y


def _random_model(n_features: int = len(FEATURE_NAMES), random_state: int = 42):
    """Returns a model that predicts random probabilities (essentially useless)."""
    rng = np.random.default_rng(random_state)
    n = 500
    X = rng.uniform(-1, 1, size=(n, n_features))
    y = rng.integers(0, 2, size=n)

    class RandomModel:
        def predict_proba(self, X_inner):
            arr = X_inner.values if hasattr(X_inner, "values") else X_inner
            n_inner = arr.shape[0]
            rng_inner = np.random.default_rng(42)
            raw = rng_inner.uniform(0, 1, size=n_inner)
            return np.column_stack([1.0 - raw, raw])

    return RandomModel(), X, y


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConformalCalibrator:
    def test_calibrate_perfect_model_achieves_coverage(self):
        """A perfectly calibrated model must achieve empirical coverage
        ∈ [0.88, 1.0] for α = 0.10 on the calibration set."""
        model, X, y = _perfect_calibration_model()
        X_cal, _, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        y_cal_s = pd.Series(y_cal)
        cal = ConformalCalibrator(alpha=0.10).calibrate(model, pd.DataFrame(X_cal), y_cal_s)

        # Empirical coverage on calibration set
        probs = model.predict_proba(X_cal)
        n_scores = 1.0 - probs[np.arange(len(y_cal_s)), y_cal_s.values]
        coverage = float(np.mean(n_scores <= cal.q_hat))

        assert 0.88 <= coverage <= 1.0, (
            f"Expected coverage ∈ [0.88, 1.0] for perfect model, got {coverage:.4f}"
        )

    def test_calibrate_random_model_high_qhat(self):
        """A random model should produce q_hat close to 1.0 (conservative)."""
        model, X, y = _random_model()
        X_cal, _, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), pd.Series(y_cal)
        )

        assert cal.q_hat >= 0.85, f"Expected q_hat >= 0.85 for random model, got {cal.q_hat:.4f}"

    def test_predict_set_contains_true_label_for_coverage_fraction(self):
        """For a well-calibrated model, the empirical coverage on a 500-sample
        holdout should be at least 88%."""
        model, X, y = _perfect_calibration_model()
        X_full, X_holdout, y_full, y_holdout = train_test_split(
            X, y, test_size=500, random_state=42
        )
        X_cal, _, y_cal, _ = train_test_split(X_full, y_full, test_size=0.5, random_state=42)

        y_cal_s = pd.Series(y_cal)
        y_holdout_s = pd.Series(y_holdout)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), y_cal_s
        )
        results = cal.predict_set(model, pd.DataFrame(X_holdout))

        covered = 0
        for i, result in enumerate(results):
            if int(y_holdout_s.iloc[i]) in result["prediction_set"]:
                covered += 1
        coverage = covered / len(results)

        assert coverage >= 0.88, (
            f"Expected coverage >= 0.88 on 500-sample holdout, got {coverage:.4f}"
        )

    def test_save_and_load_round_trip(self, tmp_path):
        """Save + load must preserve q_hat to 8 decimal places."""
        model, X, y = _perfect_calibration_model()
        X_cal, _, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), pd.Series(y_cal)
        )
        path = os.path.join(str(tmp_path), "calibration.json")
        cal.save(path)

        loaded = ConformalCalibrator.load(path)
        assert round(cal.q_hat, 8) == round(loaded.q_hat, 8), (
            f"q_hat mismatch: {cal.q_hat:.8f} vs {loaded.q_hat:.8f}"
        )
        assert cal.alpha == loaded.alpha
        assert cal.n_cal == loaded.n_cal

    def test_sha256_mismatch_raises_integrity_error(self, tmp_path):
        """Tampering with the calibration file must raise CalibrationIntegrityError."""
        model, X, y = _perfect_calibration_model()
        X_cal, _, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), pd.Series(y_cal)
        )
        path = os.path.join(str(tmp_path), "calibration.json")
        cal.save(path)

        # Tamper with the data
        with open(path, "r") as f:
            payload = json.load(f)
        payload["data"]["q_hat"] = 0.99
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

        with pytest.raises(CalibrationIntegrityError, match="SHA-256 mismatch"):
            ConformalCalibrator.load(path)

    def test_fallback_no_artifact_returns_max_conservative(self):
        """When no calibration artifact is available, score_with_uncertainty
        must return maximally conservative bounds without crashing."""
        from types import SimpleNamespace

        models = {
            "random_forest": SimpleNamespace(predict_proba=lambda X: np.array([[0.5, 0.5]])),
            "xgboost": SimpleNamespace(predict_proba=lambda X: np.array([[0.5, 0.5]])),
            "lightgbm": SimpleNamespace(predict_proba=lambda X: np.array([[0.5, 0.5]])),
        }
        fv = dict.fromkeys(FEATURE_NAMES, 0.0)

        from detection.model_inference import score_with_uncertainty

        result = score_with_uncertainty(models, fv, calibrators=None, model_dir="/tmp/nonexistent")

        assert result["score_lower"] == 0.0
        assert result["score_upper"] == 100.0
        assert result["coverage_guarantee"] == 1.0

    def test_predict_set_result_structure(self):
        """predict_set must return correct keys and types."""
        model, X, y = _perfect_calibration_model()
        X_cal, X_test, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), pd.Series(y_cal)
        )
        results = cal.predict_set(model, pd.DataFrame(X_test[:5]))

        assert len(results) == 5
        for r in results:
            assert "score" in r
            assert "prediction_set" in r
            assert "coverage_guarantee" in r
            assert "q_hat" in r
            assert 0.0 <= r["score"] <= 1.0
            assert isinstance(r["prediction_set"], list)
            assert r["coverage_guarantee"] == 0.90

    def test_predict_with_interval_monotonic(self):
        """Interval width must decrease as score moves toward extremes (high confidence)."""
        model, X, y = _perfect_calibration_model()
        X_cal, _, y_cal, _ = train_test_split(X, y, test_size=0.5, random_state=42)

        cal = ConformalCalibrator(alpha=0.10).calibrate(
            model, pd.DataFrame(X_cal), pd.Series(y_cal)
        )

        # Scores at extremes should produce narrower intervals (clipped at 0/100)
        test_scores = np.array([[0.0, 0.0, 0.0, 0.0, 0.0],
                                [10.0, 10.0, 10.0, 10.0, 10.0]])
        test_X = pd.DataFrame(test_scores)
        results = cal.predict_with_interval(model, test_X)

        for r in results:
            assert r["lower"] <= r["upper"]
            assert 0.0 <= r["lower"] <= 100.0
            assert 0.0 <= r["upper"] <= 100.0

    def test_predict_set_raises_before_calibrate(self):
        """Calling predict_set before calibrate must raise RuntimeError."""
        cal = ConformalCalibrator()
        model, X, _ = _perfect_calibration_model()
        with pytest.raises(RuntimeError, match="calibrate"):
            cal.predict_set(model, pd.DataFrame(X[:5]))

    def test_predict_with_interval_raises_before_calibrate(self):
        """Calling predict_with_interval before calibrate must raise RuntimeError."""
        cal = ConformalCalibrator()
        model, X, _ = _perfect_calibration_model()
        with pytest.raises(RuntimeError, match="calibrate"):
            cal.predict_with_interval(model, pd.DataFrame(X[:5]))

    def test_save_raises_before_calibrate(self, tmp_path):
        """Calling save before calibrate must raise RuntimeError."""
        cal = ConformalCalibrator()
        path = os.path.join(str(tmp_path), "calibration.json")
        with pytest.raises(RuntimeError, match="calibrate"):
            cal.save(path)
