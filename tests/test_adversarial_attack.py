import math

import numpy as np

from detection.adversarial_attack import fgsm_attack, pgd_attack
from detection.feature_engineering import FEATURE_NAMES
from detection.counterfactual_constraints import FEATURE_CONSTRAINTS


class DummyModel:
    def __init__(self, w=1.0, b=0.0):
        self.w = w
        self.b = b

    def predict_proba(self, X):
        # simple logistic on sum of features
        s = np.sum(X.values, axis=1) * self.w + self.b
        probs = 1 / (1 + np.exp(-s))
        return np.vstack([(1 - probs), probs]).T


def make_models():
    return {"dummy": DummyModel(w=0.5, b=-51.0)}


def base_vector():
    # non-zero mutable features and an immutable one
    v = {f: 0.1 for f in FEATURE_NAMES}
    if "account_age_days" in v:
        v["account_age_days"] = 100.0
    return v


def test_fgsm_respects_constraints():
    models = make_models()
    vec = base_vector()
    pert, p = fgsm_attack(vec, models, epsilon=0.05)
    # at least one mutable feature changed
    mutable_feats = [f for f in FEATURE_NAMES if FEATURE_CONSTRAINTS.get(f, {}).get("mutable", True)]
    changed = sum(1 for f in mutable_feats if abs(pert[f] - vec[f]) > 1e-8)
    assert changed >= 1
    # immutable features unchanged
    for f, c in FEATURE_CONSTRAINTS.items():
        if not c.get("mutable", True):
            assert abs(pert[f] - vec[f]) < 1e-8
    # bounds respected
    for f in FEATURE_NAMES:
        c = FEATURE_CONSTRAINTS.get(f, {})
        assert pert[f] >= c.get("min", -math.inf)
        assert pert[f] <= c.get("max", math.inf)


def test_pgd_lower_than_fgsm():
    models = make_models()
    vec = base_vector()
    pert_f, pf = fgsm_attack(vec, models, epsilon=0.1)
    pert_p, pp = pgd_attack(vec, models, epsilon=0.1, alpha=0.02, steps=10)
    assert pp <= pf + 1e-8


def test_asr_positive_at_large_eps():
    models = make_models()
    vec = base_vector()
    # craft a dataset of 5 positive examples
    rows = [vec.copy() for _ in range(5)]
    flipped = 0
    for r in rows:
        pert, p = pgd_attack(r, models, epsilon=0.5, alpha=0.05, steps=10)
        if p < 0.5:
            flipped += 1
    assert flipped > 0
