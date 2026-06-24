---
title: "Replace Benford Chi-Square Asymptotic P-Values with Monte Carlo Bootstrap for Small Samples"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Extend `detection/benford_engine.py` to replace asymptotic chi-square p-values (which are inaccurate for N<100 transactions) with bootstrapped Monte Carlo p-values. For each wallet window with fewer than 100 transactions, generate 10,000 bootstrap samples from the expected Benford distribution and compute an empirical p-value by comparing the observed chi-square statistic to the bootstrap distribution. This eliminates false positives and false negatives caused by asymptotic approximation failures in small-sample regimes common in SDEX wallet analysis.

## Background & Context

LedgerLens's Benford engine computes chi-square statistics comparing observed leading-digit distributions against the expected Benford distribution. The asymptotic chi-square p-value is derived from the chi-square distribution with 8 degrees of freedom (`df = digits(1-9) - 1`). This approximation is valid when `N * p_i >= 5` for all digit classes `i` — i.e., when the expected count for each digit exceeds 5. For `p_1 ≈ 0.301` (the most frequent digit), this requires `N >= 5/0.046 ≈ 109` transactions (using the rarest digit `p_9 ≈ 0.046`).

Many SDEX wallets have fewer than 100 transactions in any given window (especially the 1h and 4h windows). For these wallets, the asymptotic p-value is unreliable — it systematically over-rejects benign wallets (false positives) because the chi-square approximation breaks down in small-sample tail regions.

Monte Carlo bootstrapping solves this by directly approximating the null distribution: generate 10,000 synthetic leading-digit samples from the true Benford distribution (multinomial sampling with the expected digit probabilities), compute the chi-square statistic for each, and count what fraction exceeds the observed statistic. This empirical fraction is the p-value, valid regardless of sample size.

This change affects all 5 time windows (1h, 4h, 24h, 7d, 30d) for the chi-square feature only. Z-score and MAD features are not chi-square-based and do not require this change. The 7d and 30d windows typically have N>100 and can continue using the asymptotic formula for performance — the bootstrap is applied only when N<100.

## Objectives

- [ ] Add `BENFORD_BOOTSTRAP_THRESHOLD: int = 100` constant to `detection/benford_engine.py` (configurable via env/settings).
- [ ] Add `BENFORD_BOOTSTRAP_SAMPLES: int = 10_000` constant (configurable).
- [ ] Implement `bootstrap_chi_square_pvalue(observed_counts: np.ndarray, n_samples: int = 10_000, seed: Optional[int] = None) -> float` function.
- [ ] The function generates 10,000 multinomial samples of size `N` using Benford expected probabilities, computes chi-square for each, and returns the fraction exceeding the observed chi-square.
- [ ] Modify `BenfordEngine.compute_chi_square_pvalue(counts, N)` to call `bootstrap_chi_square_pvalue` when `N < BENFORD_BOOTSTRAP_THRESHOLD`, else use `scipy.stats.chi2.sf` as before.
- [ ] Add `pvalue_method: Literal["asymptotic", "bootstrap"]` field to the `BenfordFeatures` dataclass so callers know which method was used.
- [ ] Cache bootstrap p-values: same `(N, observed_chi_sq)` should not be recomputed; use an LRU cache with `maxsize=512`.
- [ ] Implement a vectorised batch bootstrap using `np.random.Generator.multinomial` for performance: generate all 10,000 samples in one call, not in a Python loop.
- [ ] Add `--bootstrap-threshold` and `--bootstrap-samples` options to any CLI commands that trigger Benford analysis.
- [ ] All new code covered by tests; ensure bootstrap p-values are statistically valid (calibration test).

## Technical Requirements

### `bootstrap_chi_square_pvalue` function

```python
import numpy as np
from functools import lru_cache
from typing import Optional

BENFORD_PROBS = np.array([
    np.log10(1 + 1/d) for d in range(1, 10)
])  # [0.3010, 0.1761, 0.1249, 0.0969, 0.0792, 0.0669, 0.0580, 0.0512, 0.0458]
BENFORD_PROBS /= BENFORD_PROBS.sum()   # normalise to sum to 1.0

def chi_square_statistic(observed: np.ndarray, expected: np.ndarray) -> float:
    """Chi-square statistic for observed vs expected counts."""
    return float(np.sum((observed - expected) ** 2 / (expected + 1e-9)))

def bootstrap_chi_square_pvalue(
    observed_counts: np.ndarray,
    n_bootstrap: int = 10_000,
    seed: Optional[int] = None,
) -> float:
    """
    Monte Carlo bootstrap p-value for Benford chi-square test.
    
    Args:
        observed_counts: array of shape (9,) with leading-digit counts for digits 1-9.
        n_bootstrap: number of bootstrap samples (default 10,000).
        seed: RNG seed for reproducibility (use in tests; None in production).
    
    Returns:
        Empirical p-value in (0, 1): fraction of bootstrap chi-sq values >= observed chi-sq.
        Never returns exactly 0.0 (floor: 1/n_bootstrap).
    """
    N = int(observed_counts.sum())
    if N == 0:
        return 1.0      # No data: cannot reject null
    
    expected = BENFORD_PROBS * N
    observed_stat = chi_square_statistic(observed_counts, expected)
    
    rng = np.random.default_rng(seed)
    # Vectorised: generate all n_bootstrap samples at once → shape (n_bootstrap, 9)
    bootstrap_samples = rng.multinomial(N, BENFORD_PROBS, size=n_bootstrap)
    bootstrap_expected = BENFORD_PROBS * N   # same expected for all samples
    
    # Vectorised chi-square over all bootstrap samples
    bootstrap_stats = np.sum(
        (bootstrap_samples - bootstrap_expected) ** 2 / (bootstrap_expected + 1e-9),
        axis=1
    )
    
    # Empirical p-value (with floor to avoid p=0 reporting)
    p_value = max((bootstrap_stats >= observed_stat).mean(), 1.0 / n_bootstrap)
    return float(p_value)
```

### Modified `BenfordEngine.compute_chi_square_pvalue`

```python
from scipy.stats import chi2

BENFORD_BOOTSTRAP_THRESHOLD = int(os.getenv("BENFORD_BOOTSTRAP_THRESHOLD", "100"))
BENFORD_BOOTSTRAP_SAMPLES = int(os.getenv("BENFORD_BOOTSTRAP_SAMPLES", "10000"))

def compute_chi_square_pvalue(self, counts: np.ndarray, N: int) -> tuple[float, str]:
    """
    Returns (p_value, method) where method is 'asymptotic' or 'bootstrap'.
    Uses bootstrap when N < BENFORD_BOOTSTRAP_THRESHOLD.
    """
    if N < BENFORD_BOOTSTRAP_THRESHOLD:
        p = bootstrap_chi_square_pvalue(counts, n_bootstrap=BENFORD_BOOTSTRAP_SAMPLES)
        return p, "bootstrap"
    else:
        expected = BENFORD_PROBS * N
        stat = chi_square_statistic(counts, expected)
        p = float(chi2.sf(stat, df=8))
        return p, "asymptotic"
```

### `BenfordFeatures` dataclass update

```python
@dataclass
class BenfordWindowFeatures:
    window_hours: int
    n_transactions: int
    chi_square_stat: float
    chi_square_pvalue: float
    chi_square_pvalue_method: str    # "asymptotic" or "bootstrap"  ← NEW FIELD
    mad: float
    z_scores: list[float]           # per-digit Z-scores (9 values)
    benford_flag: bool
```

### LRU cache for performance

```python
from functools import lru_cache

@lru_cache(maxsize=512)
def _cached_bootstrap_pvalue(
    counts_tuple: tuple[int, ...],   # hashable version of counts array
    n_bootstrap: int,
    seed: Optional[int],
) -> float:
    counts = np.array(counts_tuple)
    return bootstrap_chi_square_pvalue(counts, n_bootstrap, seed)
```

Cache key is `(tuple(counts), n_bootstrap, seed)`. In production `seed=None` — all real calls share the same cache bucket, which is intentional (same counts → same p-value estimate across calls in the same process).

### Performance requirement

For N=50, `bootstrap_chi_square_pvalue` with `n_bootstrap=10,000` must complete in <500ms on a single core using vectorised `numpy` operations. The Python-loop equivalent (~5 seconds) is not acceptable.

## Security Considerations

- The bootstrap p-value introduces randomness into the detection pipeline. This is acceptable: the randomness is bounded (10,000 samples gives <1% variance in p-value estimate for typical effect sizes). However, for audit reproducibility, the `pvalue_method` field must be logged alongside the p-value so auditors know whether a flagging decision was based on bootstrap or asymptotic estimates.
- `seed` parameter must default to `None` in production to ensure fresh randomness per call; only set a seed in tests. Never expose `seed` via the API or CLI to prevent an adversary from pre-computing which trade count/distribution combinations produce low p-values with a specific seed.
- The LRU cache key includes `seed`; cache entries with `seed=None` are non-reproducible across process restarts, which is correct behaviour.

## Testing Requirements

- **Unit — calibration test**: generate 1,000 samples from the true Benford distribution (N=30); compute bootstrap p-values; assert the fraction of p-values below 0.05 is approximately 0.05 ± 0.02. This tests that the bootstrap is calibrated (not over- or under-rejecting under the null).
- **Unit — power test**: generate 100 samples from a uniform distribution (clear non-Benford); assert mean bootstrap p-value < 0.01.
- **Unit — method selection**: N=99 → method=="bootstrap"; N=100 → method=="asymptotic"; N=101 → method=="asymptotic".
- **Unit — floor p-value**: construct observed_counts that exactly match expected Benford (zero chi-sq); assert bootstrap p-value > 0.5.
- **Unit — LRU cache hit**: call with same `counts_tuple`; mock `bootstrap_chi_square_pvalue`; assert mock called only once on two identical calls.
- **Unit — vectorised performance**: N=50, n_bootstrap=10_000 completes in <500ms (use `pytest-benchmark` or `timeit`).
- **Unit — N=0 edge case**: empty counts → p_value=1.0, method="bootstrap".
- **Unit — `BenfordWindowFeatures.chi_square_pvalue_method` field**: assert field is populated correctly for both methods.
- **Integration — full pipeline with small-window wallet**: wallet with 25 trades in 1h window; assert Benford features computed with method="bootstrap".

## Documentation Requirements

- Docstrings on `bootstrap_chi_square_pvalue`, `chi_square_statistic`, and the updated `compute_chi_square_pvalue`.
- Update `README.md` Benford's Law section to document the bootstrap fallback and threshold.
- Update the Benford feature table in README to add `chi_square_pvalue_method` column.
- New section in `docs/benford_analysis.md` (create if not exists): "Small-Sample P-Value Estimation" with calibration curve and methodology rationale.
- Document `BENFORD_BOOTSTRAP_THRESHOLD` and `BENFORD_BOOTSTRAP_SAMPLES` in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `bootstrap_chi_square_pvalue` implemented with vectorised numpy (no Python loop).
- [ ] `compute_chi_square_pvalue` delegates to bootstrap for N < threshold.
- [ ] `BenfordWindowFeatures.chi_square_pvalue_method` field populated correctly.
- [ ] LRU cache implemented for bootstrap p-value reuse.
- [ ] Calibration test passes (false-positive rate ≈ 0.05 under null).
- [ ] Performance test passes (<500ms for N=50, n_bootstrap=10,000).
- [ ] Method selection boundary tests pass.
- [ ] All unit and integration tests pass; ≥90% branch coverage on benford_engine.py.
- [ ] `docs/benford_analysis.md` updated with bootstrap methodology.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have a strong background in computational statistics — specifically bootstrap methods, Monte Carlo sampling, and goodness-of-fit testing. You understand the conditions under which asymptotic chi-square p-values are valid and why they fail for small samples. Proficiency with `numpy` vectorised operations is required for the performance constraint. Familiarity with LedgerLens's Benford engine and the 5-window feature schema will accelerate the work.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., computational statistics, bootstrap methods, Python scientific computing, anomaly detection).
2. **Relevant experience**: bootstrap hypothesis testing implementations, goodness-of-fit testing, or fraud detection statistical methods you have built.
3. **Approach / thoughts**: would you use the parametric bootstrap (sample from multinomial with Benford probs) or a non-parametric bootstrap (resample observed data)? What is the tradeoff for this use case?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
