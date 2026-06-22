"""Feature constraints for counterfactual / adversarial perturbations.

This module provides a minimal `FEATURE_CONSTRAINTS` manifest used by the
adversarial attack code when the upstream counterfactual engine is not
available in the workspace.
"""
from detection.feature_engineering import FEATURE_NAMES

# Default constraints: most features are continuous ratios in [0, 1].
# A small set are treated as effectively immutable for adversarial attacks.
IMMUTABLE = {"account_age_days"}

FEATURE_CONSTRAINTS = {}
for name in FEATURE_NAMES:
    if name in IMMUTABLE:
        FEATURE_CONSTRAINTS[name] = {
            "mutable": False,
            "direction": None,
            "min": 0.0,
            "max": float("inf"),
        }
    else:
        FEATURE_CONSTRAINTS[name] = {
            "mutable": True,
            "direction": None,
            "min": 0.0,
            "max": 1.0,
        }
