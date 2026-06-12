"""Benford's Law digit-distribution analysis for transaction amounts.

Computes the chi-square statistic, per-digit Z-scores, and Mean Absolute
Deviation (MAD) of the leading-digit distribution of a set of amounts,
relative to the theoretical Benford distribution.
"""

import math

import numpy as np

DIGITS = list(range(1, 10))

# P(d) = log10(1 + 1/d) for d in 1..9
BENFORD_EXPECTED: dict[int, float] = {d: math.log10(1 + 1 / d) for d in DIGITS}


def first_digit(value: float) -> int | None:
    """Return the leading (most significant) decimal digit of `value`.

    Returns None for zero, negative, or non-finite values, which are
    excluded from Benford analysis.
    """
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    while value < 1:
        value *= 10
    while value >= 10:
        value /= 10
    return int(value)


def digit_distribution(amounts: list[float]) -> dict[int, float]:
    """Return the observed proportion of each leading digit 1-9 in `amounts`."""
    digits = [d for d in (first_digit(a) for a in amounts) if d is not None]
    n = len(digits)
    if n == 0:
        return {d: 0.0 for d in DIGITS}
    counts = {d: 0 for d in DIGITS}
    for d in digits:
        counts[d] += 1
    return {d: counts[d] / n for d in DIGITS}


def chi_square_statistic(observed: dict[int, float], n: int) -> float:
    """Chi-square goodness-of-fit statistic vs. the Benford distribution.

    `observed` is a digit -> proportion mapping (e.g. from `digit_distribution`).
    `n` is the number of observations the proportions were computed from.
    """
    if n == 0:
        return 0.0
    chi_sq = 0.0
    for d in DIGITS:
        expected_count = BENFORD_EXPECTED[d] * n
        observed_count = observed.get(d, 0.0) * n
        if expected_count > 0:
            chi_sq += (observed_count - expected_count) ** 2 / expected_count
    return chi_sq


def z_scores(observed: dict[int, float], n: int) -> dict[int, float]:
    """Per-digit Z-score of the observed proportion vs. Benford's expectation."""
    if n == 0:
        return {d: 0.0 for d in DIGITS}
    scores = {}
    for d in DIGITS:
        p = BENFORD_EXPECTED[d]
        observed_p = observed.get(d, 0.0)
        # continuity correction as commonly used in Benford forensic analysis
        numerator = abs(observed_p - p) - (1 / (2 * n))
        denominator = math.sqrt(p * (1 - p) / n)
        scores[d] = max(numerator, 0.0) / denominator if denominator > 0 else 0.0
    return scores


def mean_absolute_deviation(observed: dict[int, float]) -> float:
    """MAD between observed and expected digit distributions.

    Values above ~0.015 (for first-digit tests) are commonly treated as
    indicating non-conformity with Benford's Law.
    """
    deviations = [abs(observed.get(d, 0.0) - BENFORD_EXPECTED[d]) for d in DIGITS]
    return float(np.mean(deviations))


def compute_benford_metrics(amounts: list[float]) -> dict:
    """Compute the full set of Benford metrics for a list of transaction amounts.

    Returns a dict with `chi_square`, `mad`, `z_scores` (per digit), the
    `observed_distribution`, and `sample_size`.
    """
    observed = digit_distribution(amounts)
    n = sum(1 for a in amounts if first_digit(a) is not None)

    return {
        "chi_square": chi_square_statistic(observed, n),
        "mad": mean_absolute_deviation(observed),
        "z_scores": z_scores(observed, n),
        "observed_distribution": observed,
        "sample_size": n,
    }


def is_anomalous(metrics: dict, mad_threshold: float = 0.015) -> bool:
    """Whether a `compute_benford_metrics` result exceeds the MAD threshold."""
    return metrics["mad"] > mad_threshold
