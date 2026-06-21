"""Bayesian Model Averaging ensemble reweighter.

Uses a Beta-conjugate prior on each classifier's accuracy.  For each
feedback record the predicted probability ``p`` is treated as a Bernoulli
success probability:

* When ``ground_truth == 1`` (confirmed wash): the model "succeeds" with
  weight ``p`` (confident correct prediction) and "fails" with weight
  ``1 - p``.
* When ``ground_truth == 0`` (confirmed clean): the model "succeeds" with
  weight ``1 - p`` and "fails" with weight ``p``.

This updates the Beta posterior as:
  alpha_m += success_weight
  beta_m  += failure_weight

The posterior mode ``(alpha - 1) / (alpha + beta - 2)`` is used as the
unnormalised weight for each model; weights are then normalised to sum to 1.0.

Prior:  Beta(alpha=2, beta=2) — weakly informative and symmetric.
"""

import json
import logging
import os

from detection.feedback_store import ScoringFeedback

logger = logging.getLogger("ledgerlens.ensemble_reweighter")

_MODEL_NAMES = ("random_forest", "xgboost", "lightgbm")
_WEIGHTS_FILENAME = "ensemble_weights.json"

# Beta prior parameters
_ALPHA_PRIOR = 2.0
_BETA_PRIOR = 2.0
_EPS = 1e-9  # clip probabilities away from 0/1


def compute_updated_weights(feedback: list[ScoringFeedback]) -> dict[str, float]:
    """Compute normalised ensemble weights using Bayesian Model Averaging.

    Args:
        feedback: Observed feedback records (may be empty).

    Returns:
        Dict mapping model name → weight, summing to 1.0.
        Returns uniform weights (1/3 each) when *feedback* is empty.
    """
    alpha: dict[str, float] = {m: _ALPHA_PRIOR for m in _MODEL_NAMES}
    beta: dict[str, float] = {m: _BETA_PRIOR for m in _MODEL_NAMES}

    for fb in feedback:
        if fb.model_name not in alpha:
            continue
        p = max(_EPS, min(1 - _EPS, fb.predicted_probability))
        if fb.ground_truth == 1:
            alpha[fb.model_name] += p        # confident correct → success
            beta[fb.model_name] += (1 - p)   # uncertainty → failure
        else:
            alpha[fb.model_name] += (1 - p)  # confident correct (low p) → success
            beta[fb.model_name] += p          # uncertainty → failure

    # Posterior mode = (alpha - 1) / (alpha + beta - 2); clip at _EPS
    raw = {m: max(_EPS, (alpha[m] - 1) / (alpha[m] + beta[m] - 2)) for m in _MODEL_NAMES}
    total = sum(raw.values())
    return {m: raw[m] / total for m in _MODEL_NAMES}


def apply_weights(weights: dict[str, float], model_dir: str) -> None:
    """Write *weights* to ``ensemble_weights.json`` inside *model_dir*.

    Args:
        weights: Output of :func:`compute_updated_weights`.
        model_dir: Directory where model artefacts are stored.
    """
    import time

    payload = {
        "random_forest": weights["random_forest"],
        "xgboost": weights["xgboost"],
        "lightgbm": weights["lightgbm"],
        "updated_at": time.time(),
    }
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, _WEIGHTS_FILENAME)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, path)  # atomic rename
    logger.info(
        "Wrote ensemble weights: rf=%.4f xgb=%.4f lgbm=%.4f",
        weights["random_forest"],
        weights["xgboost"],
        weights["lightgbm"],
    )



def apply_weights(weights: dict[str, float], model_dir: str) -> None:
    """Write *weights* to ``ensemble_weights.json`` inside *model_dir*.

    Args:
        weights: Output of :func:`compute_updated_weights`.
        model_dir: Directory where model artefacts are stored.
    """
    import time

    payload = {
        "random_forest": weights["random_forest"],
        "xgboost": weights["xgboost"],
        "lightgbm": weights["lightgbm"],
        "updated_at": time.time(),
    }
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, _WEIGHTS_FILENAME)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, path)  # atomic rename
    logger.info(
        "Wrote ensemble weights: rf=%.4f xgb=%.4f lgbm=%.4f",
        weights["random_forest"],
        weights["xgboost"],
        weights["lightgbm"],
    )
