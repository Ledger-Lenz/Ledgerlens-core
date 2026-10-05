"""Coordinate-wise trimmed-mean aggregation (Issue #1037).

Reference: Yin et al. (2018) "Byzantine-Robust Distributed Learning:
Towards Optimal Statistical Rates".

For each coordinate independently, the ``k = floor(trim_fraction * n)``
largest and ``k`` smallest values across participants are discarded and the
remaining ``n - 2k`` values are averaged.  Up to ``k`` Byzantine participants
can push any single coordinate arbitrarily far without moving the result
outside the range spanned by honest values.  Tolerance: fewer than
``trim_fraction`` of participants may be malicious, with
``0 <= trim_fraction < 0.5``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

AGGREGATION_STRATEGIES = ("fedavg", "trimmed_mean")


def trimmed_mean(updates: Sequence[np.ndarray], trim_fraction: float) -> np.ndarray:
    """Return the coordinate-wise trimmed mean of ``updates``.

    Args:
        updates: n arrays of identical shape.
        trim_fraction: fraction of values trimmed from *each* end per
            coordinate, in ``[0, 0.5)``.

    Raises:
        ValueError: on an empty input or an out-of-range ``trim_fraction``.
    """
    if not 0.0 <= trim_fraction < 0.5:
        raise ValueError(f"trim_fraction must be in [0, 0.5), got {trim_fraction}")
    if len(updates) == 0:
        raise ValueError("trimmed_mean requires at least one update")

    stacked = np.stack([np.asarray(u, dtype=float) for u in updates])
    n = stacked.shape[0]
    k = math.floor(trim_fraction * n)
    if k == 0:
        return stacked.mean(axis=0)
    ordered = np.sort(stacked, axis=0)
    return ordered[k : n - k].mean(axis=0)
