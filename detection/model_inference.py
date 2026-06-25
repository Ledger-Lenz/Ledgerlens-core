"""Real-time risk scoring: load trained models and score a feature vector.

Also provides conformal prediction intervals via ``score_with_uncertainty``
when calibration artifacts are present.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None
import json
import logging
import os
from typing import TYPE_CHECKING

import numpy as np

import config.settings as settings_module
from detection.feature_engineering import FEATURE_NAMES
from detection.gnn_model import _HAS_PYG, safe_load_gnn_checkpoint
from detection.model_signing import assert_within_model_dir, safe_joblib_load

logger = logging.getLogger("ledgerlens.model_inference")

_WEIGHTS_FILENAME = "ensemble_weights.json"
_REQUIRED_KEYS = frozenset({"random_forest", "xgboost", "lightgbm"})
_weights_mtime: float | None = None
_runtime_weights: dict[str, float] | None = None

if TYPE_CHECKING:
    from detection.conformal import ConformalCalibrator

logger = logging.getLogger("ledgerlens.model_inference")

_MODEL_FILENAMES = {
    "random_forest": "random_forest.joblib",
    "xgboost": "xgboost.joblib",
    "lightgbm": "lightgbm.joblib",
    "temporal_lstm": "temporal_lstm.joblib",
    "gnn": "gnn_model.pt",
}

_CALIBRATION_FILENAMES = {
    "random_forest": "random_forest_conformal.json",
    "xgboost": "xgboost_conformal.json",
    "lightgbm": "lightgbm_conformal.json",
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
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                data = json.load(fh)
            finally:
                if fcntl is not None:
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


def _load_models_base(model_dir: str | None = None) -> dict:
    """Load all trained models from `model_dir` (defaults to `settings.model_dir`)."""
    model_dir = model_dir or settings_module.settings.model_dir
    signing_key = settings_module.settings.model_signing_key.encode()
    models = {}
    for name, filename in _MODEL_FILENAMES.items():
        if name == "gnn":
            continue
        path = os.path.join(model_dir, filename)
        if os.path.exists(path):
            assert_within_model_dir(path, model_dir)
            models[name] = safe_joblib_load(path, signing_key)

    gnn_path = os.path.join(model_dir, _MODEL_FILENAMES["gnn"])
    if os.path.exists(gnn_path) and _HAS_PYG:
        try:
            models["gnn"] = safe_load_gnn_checkpoint(gnn_path)
        except RuntimeError as exc:
            logger.error("GNN checkpoint failed validation: %s", exc)
            raise

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
        if name in _NON_VOTING_MODELS:
            continue
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


def _score_feature_matrix_base(
    models: dict,
    feature_vectors: list[dict],
    **kwargs,
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
        if name in _NON_VOTING_MODELS:
            continue
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


from detection.gnn_model import safe_load_gnn_checkpoint, _HAS_PYG  # noqa: E402
from ingestion.graph_builder import TemporalGraphBuilder  # noqa: E402

_MODEL_FILENAMES = dict(globals().get("_MODEL_FILENAMES", {}))
_MODEL_FILENAMES["gnn"] = "gnn_model.pt"
_MODEL_FILENAMES["sequence_model"] = "temporal_model.pt"

# Models excluded from the tabular ensemble probability vote.
_NON_VOTING_MODELS = frozenset({"gnn", "temporal_lstm", "sequence_model"})


def load_models(model_dir: str | None = None, *args, **kwargs) -> dict:
    """Wraps the base model loader, also loading gnn_model.pt and temporal_model.pt if present."""
    actual_model_dir = model_dir or settings_module.settings.model_dir
    models = _load_models_base(actual_model_dir, *args, **kwargs)

    gnn_path = os.path.join(actual_model_dir, _MODEL_FILENAMES["gnn"])
    if os.path.exists(gnn_path) and _HAS_PYG:
        try:
            models["gnn"] = safe_load_gnn_checkpoint(gnn_path)
        except RuntimeError as e:
            logger.error("GNN checkpoint failed validation: %s", e)
            raise

    seq_path = os.path.join(actual_model_dir, _MODEL_FILENAMES["sequence_model"])
    if os.path.exists(seq_path):
        try:
            from detection.temporal_model import load_sequence_model
            s = settings_module.settings
            seq_model = load_sequence_model(
                actual_model_dir,
                model_type=s.temporal_model_type,
                lstm_hidden_dim=s.temporal_lstm_hidden_dim,
                max_seq_len=s.temporal_max_seq_len,
            )
            if seq_model is not None:
                models["sequence_model"] = seq_model
        except Exception as exc:
            logger.warning("Could not load temporal_model.pt: %s — skipping", exc)

    return models


def fuse_sequence_score(
    tabular_prob: float,
    seq_prob: float,
    w_seq: float,
) -> float:
    """Fuse tabular ensemble probability with sequence model probability.

    Uses the same linear interpolation approach as GNN fusion.
    ``w_seq`` is found by ``scipy.optimize.minimize_scalar`` on validation
    AUC-PR; valid range is [0.0, 0.4].

    Args:
        tabular_prob: Ensemble tabular probability (0–1).
        seq_prob:     WashTradeSequenceModel output probability (0–1).
        w_seq:        Learned fusion weight in [0.0, 0.4].

    Returns:
        Blended probability in [0, 1].
    """
    w = max(0.0, min(0.4, w_seq))
    return (1.0 - w) * tabular_prob + w * seq_prob


def score_feature_matrix(batch, models: dict, *args, **kwargs):
    """Wraps the base scorer, computing GNN features per-batch first.

    Benchmark target: T-GNN forward pass adds <= 200ms / 500-wallet batch
    on a single CPU core.
    """
    gnn_features = {}
    if "gnn" in models and _HAS_PYG:
        builder = TemporalGraphBuilder()
        trades = _trades_from_batch(batch)  # noqa: F821
        snapshots = builder.build_snapshots(trades, lookback_days=1)
        gnn_features = _gnn_forward_pass(models["gnn"], snapshots)

    return _score_feature_matrix_base(
        batch, models, *args, use_gnn=bool(gnn_features), gnn_features=gnn_features, **kwargs
    )


def _gnn_forward_pass(model, snapshots) -> dict:
    """Runs the T-GNN over snapshots, returns per-wallet GNN feature dict."""
    import torch
    results = {}
    model.eval()
    with torch.no_grad():
        for snap in snapshots:
            if snap.edge_index.shape[1] == 0:
                continue
            x = torch.tensor(snap.node_features, dtype=torch.float32)
            edge_index = torch.tensor(snap.edge_index, dtype=torch.long)
            edge_attr = torch.tensor(snap.edge_attr, dtype=torch.float32)
            edge_time = torch.zeros(edge_index.shape[1])
            scores = model(x, edge_index, edge_attr, edge_time)
            neighbor_avg = model.neighbor_avg_score(scores, edge_index, x.shape[0])
            for addr, idx in snap.wallet_index.items():
                results[addr] = {
                    "gnn_wash_ring_probability": float(scores[idx].item()),
                    "gnn_neighbor_avg_score": float(neighbor_avg[idx].item()),
                }
    return results


_NON_VOTING_MODELS = frozenset({"gnn", "temporal_lstm"})


class ModelInference:
    """Thin stateless wrapper around :func:`load_models` and :func:`score_feature_vector`.

    Provides a single :meth:`score` method that returns a :class:`~detection.risk_score.RiskScore`
    given a pre-built feature dict, making it easy to inject in tests.
    """

    def __init__(self, models: dict) -> None:
        self._models = models

    def score(self, wallet: str, asset_pair: str, features: dict):
        from detection.risk_score import RiskScore

        prob, confidence = score_feature_vector(self._models, features)
        benford_mad = features.get("benford_mad_24h", 0.0)
        from config.settings import settings as _settings
        return RiskScore.combine(
            wallet=wallet,
            asset_pair=asset_pair,
            benford_mad=benford_mad,
            benford_mad_threshold=_settings.benford_mad_threshold,
            ml_probability=prob,
            ml_confidence=confidence,
        )


class IncrementalScorer:
    """Stateful scorer that processes one trade at a time.

    Maintains a :class:`~detection.rolling_window.RollingWindowState` and emits
    a :class:`~detection.risk_score.RiskScore` for a wallet only when the new
    score differs from the last emitted score by at least
    *score_delta_threshold* points.

    The first trade for a wallet always emits a score (no prior baseline).

    Args:
        window_state: The in-memory rolling-window store.
        feature_engineering: A :class:`~detection.feature_engineering.FeatureEngineering` instance.
        model_inference: A :class:`ModelInference` instance wrapping the loaded models.
        score_delta_threshold: Minimum absolute score change required to emit a new score (default 5).
    """

    def __init__(
        self,
        window_state,
        feature_engineering,
        model_inference: "ModelInference",
        score_delta_threshold: int = 5,
    ) -> None:
        from detection.rolling_window import RollingWindowState
        from detection.feature_engineering import FeatureEngineering

        self._window: RollingWindowState = window_state
        self._fe: FeatureEngineering = feature_engineering
        self._infer: ModelInference = model_inference
        self._delta = score_delta_threshold
        self._last_scores: dict[str, int] = {}

    @property
    def window_state(self):
        return self._window

    def score_on_trade(self, trade):
        """Update rolling window with *trade* and return a :class:`~detection.risk_score.RiskScore`
        if ``|new_score - last_score| >= delta``, else ``None``.
        """
        wallet = trade.base_account
        self._window.add_trade(wallet, trade)

        features = self._fe.compute_incremental(
            wallet=wallet,
            trades_1h=self._window.get_window(wallet, 1),
            trades_4h=self._window.get_window(wallet, 4),
            trades_24h=self._window.get_window(wallet, 24),
        )
        new_score = self._infer.score(wallet, trade.asset_pair, features)
        last = self._last_scores.get(wallet, -999)

        if abs(new_score.score - last) >= self._delta:
            self._last_scores[wallet] = new_score.score
            return new_score
        return None
