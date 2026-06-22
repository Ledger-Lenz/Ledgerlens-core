"""Real-time risk scoring: load trained models and score a feature vector.

Also provides conformal prediction intervals via ``score_with_uncertainty``
when calibration artifacts are present.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import joblib
import numpy as np

import config.settings as settings_module
from detection.feature_engineering import FEATURE_NAMES

if TYPE_CHECKING:
    from detection.conformal import ConformalCalibrator

logger = logging.getLogger("ledgerlens.model_inference")

_MODEL_FILENAMES = {
    "random_forest": "random_forest.joblib",
    "xgboost": "xgboost.joblib",
    "lightgbm": "lightgbm.joblib",
}

_CALIBRATION_FILENAMES = {
    "random_forest": "random_forest_conformal.json",
    "xgboost": "xgboost_conformal.json",
    "lightgbm": "lightgbm_conformal.json",
}


def load_models(model_dir: str | None = None) -> dict:
    """Load all trained models from `model_dir` (defaults to `settings.model_dir`)."""
    model_dir = model_dir or settings_module.settings.model_dir
    models = {}
    for name, filename in _MODEL_FILENAMES.items():
        path = os.path.join(model_dir, filename)
        if os.path.exists(path):
            models[name] = joblib.load(path)
    if not models:
        raise FileNotFoundError(f"No trained models found in {model_dir}. Run model_training first.")
    return models


def load_calibration(model_dir: str | None = None) -> dict[str, ConformalCalibrator]:
    """Load calibration artifacts for each model, returning a dict keyed by model name.

    Missing or corrupt artifacts are logged and skipped — never raised.
    Returns an empty dict when no calibration files exist.
    """
    from detection.conformal import CalibrationIntegrityError, ConformalCalibrator

    model_dir = model_dir or settings_module.settings.model_dir
    calibrators: dict[str, ConformalCalibrator] = {}
    for name, filename in _CALIBRATION_FILENAMES.items():
        path = os.path.join(model_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            calibrators[name] = ConformalCalibrator.load(path)
        except CalibrationIntegrityError:
            logger.warning("Calibration artifact %s failed integrity check; skipping", path)
        except Exception:
            logger.warning("Failed to load calibration artifact %s; skipping", path)
    return calibrators


def _get_ensemble_weights() -> dict[str, float]:
    settings = settings_module.settings
    return {
        "random_forest": settings.ensemble_weight_rf,
        "xgboost": settings.ensemble_weight_xgb,
        "lightgbm": settings.ensemble_weight_lgbm,
    }


def score_feature_vector(models: dict, feature_vector: dict) -> tuple[float, float]:
    """Return `(probability, confidence)` for a single feature vector.

    `probability` is the weighted-average ensemble probability of a wash
    trade pattern. `confidence` is the agreement between models (1.0 =
    full agreement, lower = models disagree).
    """
    X = np.array([[feature_vector[name] for name in FEATURE_NAMES]])

    probabilities = {}
    for name, model in models.items():
        if hasattr(model, "feature_names_in_"):
            ordered = X[:, [FEATURE_NAMES.index(f) for f in model.feature_names_in_]]
        else:
            ordered = X
        probabilities[name] = model.predict_proba(ordered)[0, 1]

    weights = _get_ensemble_weights()
    total_weight = sum(weights[n] for n in probabilities)
    if total_weight <= 0:
        raise ValueError("At least one loaded model must have a positive ensemble weight.")
    weighted_prob = sum(probabilities[n] * weights[n] for n in probabilities) / total_weight

    confidence = 1.0 - float(np.std(list(probabilities.values())))
    return float(weighted_prob), max(0.0, min(1.0, confidence))


def score_feature_matrix(
    models: dict,
    feature_vectors: list[dict],
) -> list[tuple[float, float]]:
    """Score a batch of feature vectors with a single `predict_proba` call per model.

    For N accounts this makes len(models) predict_proba calls on an N-row
    matrix instead of N × len(models) calls, reducing Python overhead and
    enabling scikit-learn's internal parallelism.

    Returns a list of (probability, confidence) tuples, one per input vector,
    in the same order as `feature_vectors`. Results are numerically identical
    to calling `score_feature_vector` for each vector individually.
    """
    if not feature_vectors:
        return []

    X = np.array([[fv[name] for name in FEATURE_NAMES] for fv in feature_vectors])
    weights = _get_ensemble_weights()

    model_probs: dict[str, np.ndarray] = {}
    for name, model in models.items():
        if hasattr(model, "feature_names_in_"):
            col_idx = [FEATURE_NAMES.index(f) for f in model.feature_names_in_]
            ordered = X[:, col_idx]
        else:
            ordered = X
        model_probs[name] = model.predict_proba(ordered)[:, 1]

    total_weight = sum(weights[n] for n in model_probs)
    if total_weight <= 0:
        raise ValueError("At least one loaded model must have a positive ensemble weight.")

    weighted_probs = sum(model_probs[n] * weights[n] for n in model_probs) / total_weight

    all_probs = np.stack(list(model_probs.values()), axis=0)  # (M, N)
    confidences = np.clip(1.0 - np.std(all_probs, axis=0), 0.0, 1.0)  # (N,)

    return [(float(weighted_probs[i]), float(confidences[i])) for i in range(len(feature_vectors))]


def score_with_uncertainty(
    models: dict,
    feature_vector: dict,
    calibrators: dict[str, ConformalCalibrator] | None = None,
    model_dir: str | None = None,
) -> dict:
    """Score a single feature vector and return uncertainty estimates.

    Returns the same ``(probability, confidence)`` pair as
    :func:`score_feature_vector`, plus:

    - ``score_lower`` / ``score_upper``:  0-100 prediction interval bounds
    - ``prediction_set``:  list of class indices in the conformal set
    - ``coverage_guarantee``: target coverage (1 - alpha)

    When calibration artifacts are unavailable (``calibrators`` is ``None``
    or empty), returns maximally conservative bounds
    ``(score_lower=0.0, score_upper=100.0, coverage_guarantee=1.0)``
    without crashing.
    """
    probability, confidence = score_feature_vector(models, feature_vector)
    score_0_100 = probability * 100.0

    cal = calibrators or load_calibration(model_dir=model_dir)
    if not cal:
        return {
            "score": score_0_100,
            "score_lower": 0.0,
            "score_upper": 100.0,
            "prediction_set": [],
            "coverage_guarantee": 1.0,
        }

    # Use the most conservative (largest) q_hat across all calibrated models
    q_hat = max(c.q_hat for c in cal.values() if c.q_hat is not None)
    alpha = next(iter(cal.values())).alpha

    score_lower = max(0.0, score_0_100 - q_hat * 100.0)
    score_upper = min(100.0, score_0_100 + q_hat * 100.0)

    # Prediction set: class 1 is included if 1 - prob[1] <= q_hat
    prediction_set = [0]
    if (1.0 - probability) <= q_hat:
        prediction_set.append(1)

    return {
        "score": score_0_100,
        "score_lower": score_lower,
        "score_upper": score_upper,
        "prediction_set": prediction_set,
        "coverage_guarantee": 1.0 - alpha,
    }
