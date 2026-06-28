# Benford's Law Analysis in LedgerLens

This document describes how LedgerLens applies Benford's Law to detect wash-trading on the Stellar DEX, with a focus on p-value estimation and the small-sample bootstrap method introduced in the Unreleased version.

---

## Overview

LedgerLens computes three Benford metrics for each wallet/asset-pair window:

| Metric | Description |
|---|---|
| Chi-square statistic | Goodness-of-fit between observed and expected (Benford) leading-digit counts |
| Chi-square p-value | Probability of observing a statistic this large under the Benford null hypothesis |
| Mean Absolute Deviation (MAD) | Mean absolute difference between observed and expected digit proportions |
| Per-digit Z-scores | Standard-normal scores for each digit (1–9) with continuity correction |

These are computed across five rolling windows: **1h, 4h, 24h, 7d, 30d**.

---

## Small-Sample P-Value Estimation

### Why the Asymptotic Chi-Square Approximation Fails

The standard chi-square p-value is derived from the chi-square distribution with `df = 8` degrees of freedom (nine digit classes minus one). This approximation is valid when:

```
N × p_i ≥ 5   for all digit classes i
```

For the rarest digit (digit 9, `p_9 ≈ 0.046`), this requires:

```
N ≥ 5 / 0.046 ≈ 109 transactions
```

Many SDEX wallets have **fewer than 100 transactions** in any given short-duration window (particularly 1h and 4h). In this regime, the asymptotic approximation breaks down in the tail of the distribution — it systematically over-rejects benign wallets (false positives) and can under-reject wash-trading bots that exploit the instability.

### Monte Carlo Bootstrap Method

For windows with `N < BENFORD_BOOTSTRAP_THRESHOLD` (default: **100**), LedgerLens replaces the asymptotic p-value with a **Monte Carlo bootstrap p-value**:

1. Compute the observed chi-square statistic `T_obs` from the wallet's digit counts.
2. Generate `B = 10,000` synthetic digit samples of size `N` by drawing from a multinomial distribution parameterised by the theoretical Benford probabilities.
3. Compute the chi-square statistic `T_b` for each bootstrap sample.
4. The empirical p-value is:

```
p = (# bootstrap samples where T_b ≥ T_obs) / B
```

The p-value is floored at `1 / B = 0.0001` to avoid reporting `p = 0`.

This approach is **valid for any sample size** because it directly approximates the null distribution without relying on the asymptotic chi-square approximation.

### Implementation

```python
# detection/benford_engine.py

def bootstrap_chi_square_pvalue(
    observed_counts: np.ndarray,   # shape (9,) — raw digit counts
    n_bootstrap: int = 10_000,
    seed: Optional[int] = None,
) -> float:
    N = int(observed_counts.sum())
    if N == 0:
        return 1.0
    expected = BENFORD_PROBS * N
    observed_stat = _chi_sq_from_counts(observed_counts, expected)
    rng = np.random.default_rng(seed)
    # All B samples in one vectorised call — shape (B, 9)
    bootstrap_samples = rng.multinomial(N, BENFORD_PROBS, size=n_bootstrap)
    bootstrap_stats = np.sum(
        (bootstrap_samples - expected) ** 2 / (expected + 1e-9), axis=1
    )
    return max(float((bootstrap_stats >= observed_stat).mean()), 1.0 / n_bootstrap)
```

The vectorised NumPy implementation completes in **< 500 ms** for `N = 50`, `B = 10,000` on a single core. The Python-loop equivalent takes approximately 5 seconds and is not acceptable in production.

### Calibration

Under the true Benford null hypothesis, a correctly calibrated test at significance level `α` should reject the null in approximately `α × 100%` of cases. The bootstrap implementation was validated as follows:

- **Procedure**: Generate 1,000 independent samples of `N = 30` from the Benford distribution; compute bootstrap p-values for each; measure the fraction below `α = 0.05`.
- **Result**: The empirical false-positive rate falls in `[0.03, 0.07]` — consistent with the nominal 5% level.

This confirms that the bootstrap is **calibrated**: it neither over-rejects (false positives) nor under-rejects (false negatives) under the null.

### Method Selection Summary

| Condition | Method | Notes |
|---|---|---|
| `N < BENFORD_BOOTSTRAP_THRESHOLD` (default 100) | `"bootstrap"` | Empirical p-value; valid for any N |
| `N ≥ BENFORD_BOOTSTRAP_THRESHOLD` | `"asymptotic"` | `scipy.stats.chi2.sf(stat, df=8)` |

The method used is recorded in the `pvalue_method` field of `compute_benford_metrics` output and the `chi_square_pvalue_method` field of `BenfordWindowFeatures`, so audit logs always indicate which approximation backed a given flagging decision.

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `BENFORD_BOOTSTRAP_THRESHOLD` | `100` | N below which bootstrap is used |
| `BENFORD_BOOTSTRAP_SAMPLES` | `10000` | Number of bootstrap replicates |

Override via `.env` or shell environment. CLI overrides are also available via `ledgerlens score --bootstrap-threshold N --bootstrap-samples B`.

---

## Security Considerations

- The `seed` parameter defaults to `None` in production, ensuring fresh randomness per call. Only set a seed in tests.
- Never expose `seed` via the API or CLI — doing so would allow an adversary to pre-compute which (count, distribution) combinations produce low p-values for a chosen seed.
- The LRU cache key includes `seed`, so cached entries with `seed=None` are process-local and not reproducible across restarts (correct behaviour for production).
- Because the bootstrap introduces bounded randomness, the `pvalue_method` field must be logged alongside the p-value so auditors can distinguish deterministic (asymptotic) from stochastic (bootstrap) flagging decisions.

---

## References

- Benford, F. (1938) "The law of anomalous numbers", *Proceedings of the American Philosophical Society*, 78(4), pp. 551–572.
- Pearson, K. (1900) "On the criterion that a given system of deviations from the probable in the case of a correlated system of variables is such that it can be reasonably supposed to have arisen from random sampling", *Philosophical Magazine*, 50(302), pp. 157–175.
- Good, P.I. (2005) *Permutation, Parametric, and Bootstrap Tests of Hypotheses*, 3rd ed. Springer.
