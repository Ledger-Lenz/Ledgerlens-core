"""Per-round FedAvg weight-share capping.

A defense-in-depth layer on top of participant admission (see `admission.py`):
even an admitted participant's aggregation weight, computed from its
(admission-ceiling-clamped) claimed `n_samples`, must not exceed
`FEDERATED_MAX_PARTICIPANT_WEIGHT_FRACTION` of a round's *total* weight. This
guards the case where quorum is small or an operator's own admission ceiling
was itself set too generously -- a single round should never be dominated by
one voice, independent of what any individual identity was approved to claim.

This is explicitly a stopgap layered *on top of* admission control, not a
replacement for it: capping a fraction here bounds one round's damage from a
single identity, but a determined attacker who can register many identities
(which admission control, not this module, is responsible for preventing)
could still approach the cap collectively across several of them.
"""

from __future__ import annotations

import numpy as np


def apply_weight_share_cap(raw_weights: np.ndarray, cap: float) -> np.ndarray:
    """Return weights redistributed so no entry exceeds `cap` of the total.

    `raw_weights` need not already sum to 1 -- it is normalized first. Uses
    iterative water-filling: repeatedly clamp any over-cap entry to `cap` and
    redistribute the freed weight proportionally among the still-uncapped
    entries (by their relative raw weights), until no entry exceeds `cap`.
    Converges in at most `len(raw_weights)` iterations, since at least one
    additional entry becomes permanently capped each iteration that makes a
    change.

    `cap < 1` participants can never all simultaneously satisfy `cap` (their
    weights must sum to 1, so the average is `1/n`) -- in that regime `cap`
    is relaxed to `1/n`, which is the tightest cap that is always feasible
    regardless of the input distribution (equal-weighting everyone). This
    means a configured cap is a *ceiling* on how tight capping can get, not a
    guarantee that every round is capped at exactly that value when quorum
    is small.

    No-ops (returns the normalized weights unchanged) when there are 0 or 1
    entries, or `cap >= 1.0` -- capping a single participant's round to
    anything less than 100% is meaningless (there is nowhere to redistribute
    to), and `cap >= 1.0` means "no cap" by definition.
    """
    n = len(raw_weights)
    if n == 0:
        return np.asarray(raw_weights, dtype=float)

    weights = np.asarray(raw_weights, dtype=float)
    total = weights.sum()
    if total <= 0:
        return weights
    weights = weights / total

    if n <= 1 or cap >= 1.0:
        return weights

    effective_cap = max(cap, 1.0 / n)
    result = weights.copy()
    capped_mask = np.zeros(n, dtype=bool)

    for _ in range(n):
        over = (~capped_mask) & (result > effective_cap + 1e-12)
        if not np.any(over):
            break
        result[over] = effective_cap
        capped_mask |= over

        free_mask = ~capped_mask
        if not np.any(free_mask):
            break
        remaining_total = 1.0 - result[capped_mask].sum()
        free_raw_sum = weights[free_mask].sum()
        if free_raw_sum <= 0:
            result[free_mask] = remaining_total / free_mask.sum()
        else:
            result[free_mask] = weights[free_mask] / free_raw_sum * remaining_total

    return result
