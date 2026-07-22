"""Counterfactual explanation generation: "what would need to change?"

Given a wallet's current feature vector, finds the smallest feasible
perturbation that would drop the ensemble's predicted score below a target
threshold. See `docs/counterfactual_explanations.md` for the full algorithm
write-up and its limitations.

## Algorithm

The models in the ensemble (Random Forest, XGBoost, LightGBM) are all
tree-based, so their decision surface is piecewise-constant -- gradient-based
search (and therefore DiCE's default gradient mode) has no useful gradient to
follow almost everywhere. Instead this module uses a **greedy, constraint-projected
line search**:

1. Rank every mutable feature by how much moving it alone to its most
   permissive allowed value (per `FEATURE_CONSTRAINTS`) would reduce the
   predicted score.
2. For increasing cumulative subsets of the highest-impact features (and for
   each high-impact feature individually, for diversity), binary-search the
   smallest fraction `t` of the move toward each feature's extreme value that
   is jointly sufficient to push the predicted score below `target_score`.
3. Every candidate is constructed by interpolating from the observed value
   toward the constraint-permitted extreme, so it is feasible by
   construction; a defensive constraint check still discards (and skips) any
   candidate that somehow violates `FEATURE_CONSTRAINTS`.
4. Candidates are deduplicated, sorted by distance from the original vector
   (L1 or L2 norm of the feature deltas, configurable), and the closest
   `n_counterfactuals` are returned.

This assumes the predicted score moves roughly monotonically as a feature is
pushed toward its permitted extreme. That assumption can fail for tree
ensembles in principle; when it does, the binary search still returns a
*feasible* point (because the full move, t=1, is verified before searching)
but not necessarily the minimum-distance one. See the docs for more detail.
"""

from detection.counterfactual_constraints import FEATURE_CONSTRAINTS, get_mutable_features
from detection.model_inference import score_feature_vector

_CONSTRAINTS_BY_NAME = {c.feature_name: c for c in FEATURE_CONSTRAINTS}

_BISECTION_ITERATIONS = 15
_MAX_SINGLE_FEATURE_PROBES = 5
_MAX_EXTRA_CUMULATIVE_SUBSETS = 2


def _predicted_score(models: dict, feature_vector: dict) -> int:
    """Return the ensemble's prediction for `feature_vector` on the 0-100 risk-score scale."""
    probability, _confidence = score_feature_vector(models, feature_vector)
    return round(probability * 100)


def _extreme_value(name: str, original_value: float) -> float | None:
    """Return the most-permissive value `name` may take, or `None` if unbounded in that direction."""
    constraint = _CONSTRAINTS_BY_NAME[name]
    if constraint.direction == "decrease":
        return constraint.min_val
    if constraint.direction == "increase":
        return constraint.max_val
    return original_value


def _candidate_for_subset(feature_vector: dict, subset: list[str], t: float) -> dict:
    """Move every feature in `subset` a `t` (0..1) fraction of the way to its extreme value."""
    candidate = dict(feature_vector)
    for name in subset:
        original = feature_vector[name]
        extreme = _extreme_value(name, original)
        if extreme is None:
            continue
        candidate[name] = original + t * (extreme - original)
    return candidate


def _violates_constraints(candidate: dict, original: dict) -> bool:
    """Return True if `candidate` is infeasible under `FEATURE_CONSTRAINTS`."""
    tolerance = 1e-9
    for name, value in candidate.items():
        constraint = _CONSTRAINTS_BY_NAME.get(name)
        if constraint is None:
            continue
        if not constraint.mutable:
            if abs(value - original[name]) > tolerance:
                return True
            continue
        if constraint.direction == "decrease" and value > original[name] + tolerance:
            return True
        if constraint.direction == "increase" and value < original[name] - tolerance:
            return True
        if constraint.min_val is not None and value < constraint.min_val - tolerance:
            return True
        if constraint.max_val is not None and value > constraint.max_val + tolerance:
            return True
    return False


def _distance(feature_vector: dict, candidate: dict, mutable_features: list[str], norm: str) -> float:
    """Return the L1 or L2 norm of `candidate`'s deltas from `feature_vector` over `mutable_features`."""
    deltas = [candidate[name] - feature_vector[name] for name in mutable_features if candidate[name] != feature_vector[name]]
    if norm == "l1":
        return float(sum(abs(d) for d in deltas))
    return float(sum(d * d for d in deltas) ** 0.5)


def _best_t_for_subset(
    models: dict, feature_vector: dict, subset: list[str], target_score: int
) -> float | None:
    """Binary-search the smallest `t` in [0, 1] for which moving `subset` toward its
    extreme values by fraction `t` scores strictly below `target_score`.

    Returns `None` if even the full move (`t=1`) is insufficient.
    """
    full_candidate = _candidate_for_subset(feature_vector, subset, 1.0)
    if _predicted_score(models, full_candidate) >= target_score:
        return None

    lo, hi = 0.0, 1.0
    for _ in range(_BISECTION_ITERATIONS):
        mid = (lo + hi) / 2
        candidate = _candidate_for_subset(feature_vector, subset, mid)
        if _predicted_score(models, candidate) < target_score:
            hi = mid
        else:
            lo = mid
    return hi


def _rank_features_by_impact(models: dict, feature_vector: dict, mutable_features: list[str]) -> list[str]:
    """Rank mutable features by how much a full move to their extreme value alone
    reduces the predicted score (largest reduction first).
    """
    baseline = _predicted_score(models, feature_vector)
    impacts = []
    for name in mutable_features:
        candidate = _candidate_for_subset(feature_vector, [name], 1.0)
        reduction = baseline - _predicted_score(models, candidate)
        impacts.append((reduction, name))
    impacts.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _reduction, name in impacts]


def _build_candidate_result(
    feature_vector: dict, models: dict, subset: list[str], t: float, mutable_features: list[str], norm: str
) -> dict | None:
    """Build the result dict for one candidate, or `None` if it is infeasible or empty."""
    candidate = _candidate_for_subset(feature_vector, subset, t)
    if _violates_constraints(candidate, feature_vector):
        return None

    feature_deltas = {
        name: candidate[name] - feature_vector[name]
        for name in mutable_features
        if abs(candidate[name] - feature_vector[name]) > 1e-12
    }
    if not feature_deltas:
        return None

    return {
        "counterfactual_features": candidate,
        "feature_deltas": feature_deltas,
        "predicted_score": _predicted_score(models, candidate),
        "distance": _distance(feature_vector, candidate, mutable_features, norm),
        "feasible": True,
    }


def generate_counterfactuals(
    feature_vector: dict,
    models: dict,
    n_counterfactuals: int = 3,
    target_score: int | None = None,
    norm: str = "l2",
) -> list[dict]:
    """Find up to `n_counterfactuals` minimal, feasible perturbations of `feature_vector`
    that would drop the ensemble's predicted score below `target_score`.

    `target_score` defaults to `settings.risk_score_threshold - 1`. `norm` selects
    the distance metric used to rank candidates ("l1" or "l2", default "l2").

    Returns an empty list if `feature_vector` already scores below `target_score`,
    or if no feasible counterfactual exists within `FEATURE_CONSTRAINTS`.
    """
    from config.settings import get_runtime_risk_score_threshold

    target_score = target_score if target_score is not None else get_runtime_risk_score_threshold() - 1

    current_score = _predicted_score(models, feature_vector)
    if current_score < target_score:
        return []

    mutable_features = get_mutable_features()
    ranked = _rank_features_by_impact(models, feature_vector, mutable_features)

    candidates: list[dict] = []

    # Individual single-feature probes (diversity: "just change this one thing").
    for name in ranked[:_MAX_SINGLE_FEATURE_PROBES]:
        t = _best_t_for_subset(models, feature_vector, [name], target_score)
        if t is None:
            continue
        result = _build_candidate_result(feature_vector, models, [name], t, mutable_features, norm)
        if result is not None:
            candidates.append(result)

    # Cumulative subsets of the highest-impact features, stopping shortly after
    # the first feasible subset size (smaller subsets are easier to act on).
    found_feasible_size = None
    for size in range(1, len(ranked) + 1):
        if found_feasible_size is not None and size > found_feasible_size + _MAX_EXTRA_CUMULATIVE_SUBSETS:
            break
        subset = ranked[:size]
        t = _best_t_for_subset(models, feature_vector, subset, target_score)
        if t is None:
            continue
        if found_feasible_size is None:
            found_feasible_size = size
        result = _build_candidate_result(feature_vector, models, subset, t, mutable_features, norm)
        if result is not None:
            candidates.append(result)

    # Deduplicate candidates with (near-)identical deltas, keeping the first seen.
    deduped: list[dict] = []
    seen_keys = set()
    for c in candidates:
        key = tuple(sorted((name, round(delta, 6)) for name, delta in c["feature_deltas"].items()))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(c)

    deduped.sort(key=lambda c: c["distance"])
    return deduped[:n_counterfactuals]
