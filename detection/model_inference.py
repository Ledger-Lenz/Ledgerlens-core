"""Real-time risk scoring: load trained models and score a feature vector."""

import fcntl
import json
import logging
import os

import joblib
import numpy as np

import config.settings as settings_module
from detection.feature_engineering import FEATURE_NAMES

logger = logging.getLogger("ledgerlens.model_inference")

_WEIGHTS_FILENAME = "ensemble_weights.json"
_REQUIRED_KEYS = frozenset({"random_forest", "xgboost", "lightgbm"})
_weights_mtime: float | None = None
_runtime_weights: dict[str, float] | None = None

_MODEL_FILENAMES = {
    "random_forest": "random_forest.joblib",
    "xgboost": "xgboost.joblib",
    "lightgbm": "lightgbm.joblib",
}


def load_runtime_weights(model_dir: str) -> dict[str, float] | None:
    """Load ensemble weights from ``ensemble_weights.json`` if valid.

    Validates that:
    - The file exists and is parseable JSON.
    - Exactly the three model keys are present and all weights are non-negative.
    - Weights sum to within 1e-4 of 1.0.

    Returns the weight dict on success, or ``None`` (with a WARNING log) on
    any validation failure so callers can fall back to static settings values.

    Thread safety: uses ``fcntl.flock`` for a shared (read) lock so concurrent
    writer processes (CLI reweight command) cannot produce a torn read.
    """
    global _weights_mtime, _runtime_weights

    path = os.path.join(model_dir, _WEIGHTS_FILENAME)
    if not os.path.exists(path):
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    if mtime == _weights_mtime and _runtime_weights is not None:
        return _runtime_weights

    try:
        with open(path, "r") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                data = json.load(fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ensemble_weights.json unreadable: %s — falling back to settings", exc)
        return None

    model_keys = {k: data[k] for k in _REQUIRED_KEYS if k in data}
    if set(model_keys) != _REQUIRED_KEYS:
        logger.warning(
            "ensemble_weights.json has unexpected keys %s — falling back to settings",
            set(data.keys()) - {"updated_at"},
        )
        return None

    weights: dict[str, float] = {}
    for k, v in model_keys.items():
        try:
            w = float(v)
        except (TypeError, ValueError):
            logger.warning("ensemble_weights.json: non-numeric weight for %s — falling back", k)
            return None
        if w < 0:
            logger.warning("ensemble_weights.json: negative weight for %s — falling back", k)
            return None
        weights[k] = w

    if abs(sum(weights.values()) - 1.0) > 1e-4:
        logger.warning(
            "ensemble_weights.json weights sum to %.6f (not 1.0) — falling back to settings",
            sum(weights.values()),
        )
        return None

    _weights_mtime = mtime
    _runtime_weights = weights
    return weights


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


def _get_ensemble_weights(model_dir: str | None = None) -> dict[str, float]:
    """Return ensemble weights, preferring runtime cache over static settings.

    If `settings` does not define `model_dir` (tests may monkeypatch a
    SimpleNamespace without it), fall back to static settings and skip
    attempting to load runtime weights.
    """
    actual_model_dir = model_dir if model_dir is not None else getattr(
        settings_module.settings, "model_dir", None
    )
    runtime = load_runtime_weights(actual_model_dir) if actual_model_dir else None
    if runtime is not None:
        logger.debug("Using runtime ensemble weights from ensemble_weights.json")
        return runtime
    logger.debug("Using static ensemble weights from settings")
    s = settings_module.settings
    return {
        "random_forest": s.ensemble_weight_rf,
        "xgboost": s.ensemble_weight_xgb,
        "lightgbm": s.ensemble_weight_lgbm,
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
