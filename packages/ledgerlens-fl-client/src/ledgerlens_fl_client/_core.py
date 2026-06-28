"""Core federated learning algorithms.

Extracted from detection/federated/client.py with zero monorepo dependencies.
Implements ensemble prediction, differential privacy noise injection, and
gradient clipping.
"""

from __future__ import annotations

import math

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier


def ensemble_predict_proba(
    models: dict[str, RandomForestClassifier | XGBClassifier | LGBMClassifier],
    X: np.ndarray,
    ensemble_weights: dict[str, float],
) -> np.ndarray:
    """Weighted ensemble soft-label prediction.
    
    Parameters
    ----------
    models : dict
        Dictionary mapping model name to trained classifier.
    X : np.ndarray
        Feature matrix (n_samples, n_features).
    ensemble_weights : dict
        Weights for each model type (rf, xgb, lgbm).
    
    Returns
    -------
    np.ndarray
        Weighted average of predict_proba[:, 1] across models.
    """
    total_w = sum(ensemble_weights.get(n, 0.0) for n in models)
    if total_w <= 0:
        total_w = len(models)
    
    probs = np.zeros(X.shape[0], dtype=np.float64)
    for name, model in models.items():
        w = ensemble_weights.get(name, 1.0) / total_w
        probs += w * model.predict_proba(X)[:, 1]
    
    return probs


def gaussian_sigma(sensitivity: float, epsilon: float, delta: float) -> float:
    """Compute Gaussian mechanism noise scale for (ε, δ)-DP.
    
    Parameters
    ----------
    sensitivity : float
        L2 sensitivity (clip threshold).
    epsilon : float
        Differential privacy epsilon.
    delta : float
        Differential privacy delta.
    
    Returns
    -------
    float
        Noise scale sigma, or 0.0 if epsilon/delta invalid.
    """
    if epsilon <= 0 or delta <= 0:
        return 0.0
    return sensitivity * math.sqrt(2.0 * math.log(1.25 / delta)) / epsilon


def clip_delta(delta: np.ndarray, clip_threshold: float) -> np.ndarray:
    """Clip delta L2 norm to threshold.
    
    Parameters
    ----------
    delta : np.ndarray
        Gradient/update vector.
    clip_threshold : float
        Maximum allowed L2 norm.
    
    Returns
    -------
    np.ndarray
        Clipped delta (unchanged if norm <= threshold).
    """
    norm = float(np.linalg.norm(delta))
    if norm > clip_threshold:
        delta = delta * (clip_threshold / norm)
    return delta


def inject_dp_noise(
    delta: np.ndarray,
    clip_threshold: float,
    noise_multiplier: float,
    dp_epsilon: float,
    dp_delta: float,
) -> np.ndarray:
    """Add Gaussian DP noise to delta (client-side privacy).
    
    Uses RDP path when noise_multiplier > 0, otherwise falls back to
    classical (ε, δ) Gaussian mechanism.
    
    Parameters
    ----------
    delta : np.ndarray
        Gradient/update vector.
    clip_threshold : float
        L2 clip threshold (sensitivity).
    noise_multiplier : float
        Noise multiplier for RDP path (σ = clip_threshold × nm).
    dp_epsilon : float
        Differential privacy epsilon (legacy path).
    dp_delta : float
        Differential privacy delta (legacy path).
    
    Returns
    -------
    np.ndarray
        Delta with Gaussian noise added.
    """
    if noise_multiplier > 0.0:
        sigma = clip_threshold * noise_multiplier
    else:
        sigma = gaussian_sigma(clip_threshold, dp_epsilon, dp_delta)
    
    noise = np.random.normal(0.0, sigma, delta.shape)
    return delta + noise


def train_local_ensemble(
    X: np.ndarray,
    y: np.ndarray,
    random_state: int = 42,
    prev_xgb_booster=None,
    prev_lgbm_model=None,
) -> tuple[dict, object, object]:
    """Train RF/XGB/LGBM ensemble on private data.
    
    Parameters
    ----------
    X : np.ndarray
        Feature matrix (n_samples, n_features).
    y : np.ndarray
        Labels (n_samples,).
    random_state : int
        Random seed for reproducibility.
    prev_xgb_booster : optional
        Previous XGBoost booster for warm-starting.
    prev_lgbm_model : optional
        Previous LightGBM model for warm-starting.
    
    Returns
    -------
    tuple
        (models_dict, xgb_booster, lgbm_model) for next round warm-start.
    """
    rf = RandomForestClassifier(n_estimators=100, random_state=random_state, n_jobs=-1)
    rf.fit(X, y)
    
    xgb = XGBClassifier(eval_metric="logloss", random_state=random_state, verbosity=0)
    if prev_xgb_booster is not None:
        xgb.fit(X, y, xgb_model=prev_xgb_booster)
    else:
        xgb.fit(X, y)
    
    lgbm = LGBMClassifier(random_state=random_state, verbose=-1)
    if prev_lgbm_model is not None:
        lgbm.fit(X, y, init_model=prev_lgbm_model)
    else:
        lgbm.fit(X, y)
    
    models = {"random_forest": rf, "xgboost": xgb, "lightgbm": lgbm}
    return models, xgb.get_booster(), lgbm


def update_with_distilled_labels(
    X_priv: np.ndarray,
    y_priv: np.ndarray,
    X_pub: np.ndarray,
    global_soft_labels: np.ndarray,
    ensemble_weights: dict[str, float],
    random_state: int = 42,
    prev_xgb_booster=None,
    prev_lgbm_model=None,
) -> tuple[dict, object, object]:
    """Retrain ensemble augmented with distilled labels from global model.
    
    Parameters
    ----------
    X_priv : np.ndarray
        Private feature matrix.
    y_priv : np.ndarray
        Private labels.
    X_pub : np.ndarray
        Public dataset features.
    global_soft_labels : np.ndarray
        Aggregated soft labels from server.
    ensemble_weights : dict
        Weights for each model type.
    random_state : int
        Random seed.
    prev_xgb_booster : optional
        Previous XGBoost booster.
    prev_lgbm_model : optional
        Previous LightGBM model.
    
    Returns
    -------
    tuple
        (models_dict, xgb_booster, lgbm_model) for next round.
    """
    y_distill = (global_soft_labels >= 0.5).astype(int)
    X_aug = np.vstack([X_priv, X_pub])
    y_aug = np.concatenate([y_priv, y_distill])
    
    rf = RandomForestClassifier(n_estimators=100, random_state=random_state, n_jobs=-1)
    rf.fit(X_aug, y_aug)
    
    xgb = XGBClassifier(eval_metric="logloss", random_state=random_state, verbosity=0)
    if prev_xgb_booster is not None:
        xgb.fit(X_aug, y_aug, xgb_model=prev_xgb_booster)
    else:
        xgb.fit(X_aug, y_aug)
    
    lgbm = LGBMClassifier(random_state=random_state, verbose=-1)
    if prev_lgbm_model is not None:
        lgbm.fit(X_aug, y_aug, init_model=prev_lgbm_model)
    else:
        lgbm.fit(X_aug, y_aug)
    
    models = {"random_forest": rf, "xgboost": xgb, "lightgbm": lgbm}
    return models, xgb.get_booster(), lgbm


def evaluate_ensemble(
    models: dict[str, RandomForestClassifier | XGBClassifier | LGBMClassifier],
    X_test: np.ndarray,
    y_test: np.ndarray,
    ensemble_weights: dict[str, float],
) -> float:
    """Compute ensemble AUC-ROC on held-out test set.
    
    Parameters
    ----------
    models : dict
        Trained ensemble models.
    X_test : np.ndarray
        Test feature matrix.
    y_test : np.ndarray
        Test labels.
    ensemble_weights : dict
        Model weights.
    
    Returns
    -------
    float
        AUC-ROC score.
    """
    from sklearn.metrics import roc_auc_score
    
    probs = ensemble_predict_proba(models, X_test, ensemble_weights)
    return float(roc_auc_score(y_test, probs))