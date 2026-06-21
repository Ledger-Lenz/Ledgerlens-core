"""Adversarial attack utilities for tree-ensemble models.

Implements a finite-difference gradient approximation (Option A) which is
model-agnostic and works with non-differentiable tree ensembles by
estimating directional derivatives via forward differences. The estimated
gradient is used in FGSM and PGD attacks. All perturbations respect the
`FEATURE_CONSTRAINTS` manifest (immutable, directional, bounds).

The implementation is intentionally simple and conservative: projection is
performed in L2 norm over mutable features; directional constraints are
enforced per-feature; and feature vectors are validated against
`FEATURE_NAMES` to prevent injection of unknown features.
"""
from __future__ import annotations

import math
from typing import Tuple

import numpy as np

from detection.feature_engineering import FEATURE_NAMES
try:
    from detection.counterfactual_constraints import FEATURE_CONSTRAINTS
except Exception:
    # Fallback: minimal constraints if upstream module missing
    from detection.counterfactual_constraints import FEATURE_CONSTRAINTS  # type: ignore


def _validate_feature_vector(vec: dict) -> None:
    unknown = set(vec.keys()) - set(FEATURE_NAMES)
    if unknown:
        raise ValueError(f"Unknown features in vector: {sorted(unknown)}")


def _ensemble_proba(models: dict, vec: dict) -> float:
    """Return mean predicted probability for the positive class across models."""
    import pandas as pd

    X = pd.DataFrame([vec])[FEATURE_NAMES].fillna(0.0)
    probs = []
    for name, m in models.items():
        if name == "temporal_lstm":
            continue
        try:
            probs.append(float(m.predict_proba(X)[:, 1][0]))
        except Exception:
            # Some callers may pass already-wrapped model dicts; try attribute
            probs.append(float(m.predict_proba(X)[:, 1][0]))
    return float(np.mean(probs))


def _finite_diff_grad(models: dict, vec: dict, eps: float = 1e-3) -> dict:
    """Estimate gradient of ensemble probability w.r.t. each feature.

    Uses forward difference: (f(x+eps e_i) - f(x)) / eps. Immutable features
    are given gradient 0.
    """
    base = _ensemble_proba(models, vec)
    grads = {}
    for feat in FEATURE_NAMES:
        c = FEATURE_CONSTRAINTS.get(feat, {})
        if not c.get("mutable", True):
            grads[feat] = 0.0
            continue
        pert = vec.copy()
        pert_val = pert.get(feat, 0.0) + eps
        pert[feat] = pert_val
        val = _ensemble_proba(models, pert)
        grads[feat] = (val - base) / eps
    return grads


def _apply_constraints(orig: dict, candidate: dict) -> dict:
    out = orig.copy()
    for feat, val in candidate.items():
        cons = FEATURE_CONSTRAINTS.get(feat, {})
        if not cons.get("mutable", True):
            continue
        direction = cons.get("direction")
        if direction == "decrease":
            val = min(val, orig.get(feat, 0.0))
        if direction == "increase":
            val = max(val, orig.get(feat, 0.0))
        val = max(cons.get("min", -math.inf), val)
        val = min(cons.get("max", math.inf), val)
        out[feat] = float(val)
    return out


def _project_l2(orig: dict, pert: dict, eps: float) -> dict:
    # Project delta onto L2 ball of radius eps over mutable feature indices.
    delta = []
    feats = []
    for feat in FEATURE_NAMES:
        if FEATURE_CONSTRAINTS.get(feat, {}).get("mutable", True):
            d = pert.get(feat, orig.get(feat, 0.0)) - orig.get(feat, 0.0)
            delta.append(d)
            feats.append(feat)
    delta = np.array(delta, dtype=float)
    norm_delta = np.linalg.norm(delta)
    if norm_delta <= eps or norm_delta == 0.0:
        return pert
    scaled = delta * (eps / norm_delta)
    out = orig.copy()
    for f, s in zip(feats, scaled):
        out[f] = float(orig.get(f, 0.0) + s)
    return out


def fgsm_attack(feature_vector: dict, models: dict, epsilon: float) -> Tuple[dict, float]:
    """Single-step FGSM-style attack using finite-difference gradient estimation.

    Parameters
    - feature_vector: mapping feature -> value
    - models: dict of sklearn-like classifiers with `predict_proba`
    - epsilon: L2 budget for the single-step update

    Returns (perturbed_vector, ensemble_probability).
    """
    _validate_feature_vector(feature_vector)
    orig = feature_vector.copy()
    grads = _finite_diff_grad(models, orig, eps=1e-3)
    # Build candidate by moving in signed-gradient direction
    cand = orig.copy()
    # create vector of gradients over FEATURE_NAMES
    gvec = np.array([grads.get(f, 0.0) for f in FEATURE_NAMES], dtype=float)
    if np.linalg.norm(gvec) == 0.0:
        pert = orig.copy()
        proba = _ensemble_proba(models, pert)
        return pert, proba
    # normalized signed gradient
    sgn = np.sign(gvec)
    # take step scaled to epsilon in L2
    step = sgn
    step = step / np.linalg.norm(step) * epsilon
    for f, s in zip(FEATURE_NAMES, step):
        if FEATURE_CONSTRAINTS.get(f, {}).get("mutable", True):
            cand[f] = float(orig.get(f, 0.0) + s)
    cand = _apply_constraints(orig, cand)
    cand = _project_l2(orig, cand, epsilon)
    proba = _ensemble_proba(models, cand)
    return cand, proba


def pgd_attack(feature_vector: dict, models: dict, epsilon: float, alpha: float, steps: int) -> Tuple[dict, float]:
    """Projected Gradient Descent attack using finite-difference gradients.

    - epsilon: L2 budget
    - alpha: step size per iteration (L2)
    - steps: number of iterations (bounded by caller)
    """
    if steps <= 0:
        raise ValueError("steps must be > 0")
    _validate_feature_vector(feature_vector)
    orig = feature_vector.copy()
    pert = orig.copy()
    for i in range(steps):
        grads = _finite_diff_grad(models, pert, eps=1e-3)
        gvec = np.array([grads.get(f, 0.0) for f in FEATURE_NAMES], dtype=float)
        # move opposite to gradient to reduce probability
        if np.linalg.norm(gvec) == 0.0:
            break
        step = -gvec
        step = step / np.linalg.norm(step) * alpha
        cand = pert.copy()
        for f, s in zip(FEATURE_NAMES, step):
            if FEATURE_CONSTRAINTS.get(f, {}).get("mutable", True):
                cand[f] = float(cand.get(f, 0.0) + s)
        cand = _apply_constraints(orig, cand)
        cand = _project_l2(orig, cand, epsilon)
        pert = cand
    proba = _ensemble_proba(models, pert)
    return pert, proba
