---
title: "Add Kolmogorov-Smirnov and Kuiper Tests as Additional Benford Conformity Metrics"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary
The chi-square test used in `benford_engine.py` loses statistical power for small sample sizes (N < 50) because it relies on asymptotic approximations that break down with sparse digit bins. The Kolmogorov-Smirnov (KS) test and the Kuiper test operate on cumulative distributions and remain valid for small N, making them ideal complements to chi-square for low-volume wallets and short time windows. Adding these two tests as additional Benford conformity metrics improves detection coverage for wallets with sparse trade histories.

## Background & Context
The chi-square test in `detection/benford_engine.py` tests whether the observed digit frequency distribution differs significantly from the Benford expected distribution. However:

- For N < 30, expected cell counts for digits 8 and 9 (4.6% and 5.1% of N) fall below 5, violating the chi-square applicability condition
- The chi-square statistic is sensitive to *overall* distributional differences but less sensitive to *local* deviations concentrated in one part of the distribution
- The KS test (two-sample or one-sample against the Benford CDF) is exact for finite N and does not require minimum cell counts
- The Kuiper test is a rotation-invariant variant of KS, making it more sensitive to deviations at the tails of the digit distribution (digits 1 and 9), which is precisely where wash-trading bots that use round lot sizes tend to deviate

Both statistics should be computed alongside the existing chi-square and MAD metrics and added as features in `FEATURE_NAMES`. The KS and Kuiper statistics are particularly valuable in the 1h and 4h windows where N is often below 50.

Reference implementations: `scipy.stats.kstest` (one-sample KS), but the Kuiper test is not in `scipy` and must be implemented manually or via the `astropy.stats.kuiper` module (preferred — already commonly available in scientific Python environments).

## Objectives
- [ ] Implement `compute_ks_statistic(digit_counts: np.ndarray) -> KSResult` in `benford_engine.py` using a one-sample KS test against the theoretical Benford CDF, returning the D-statistic and p-value
- [ ] Implement `compute_kuiper_statistic(digit_counts: np.ndarray) -> KuiperResult` returning the Kuiper V-statistic and p-value; use `astropy.stats.kuiper_two` or a self-contained implementation if `astropy` is unavailable
- [ ] Add `ks_stat_{window}`, `ks_pval_{window}`, `kuiper_stat_{window}`, `kuiper_pval_{window}` (20 new features, 4 per window × 5 windows) to `FEATURE_NAMES` in `detection/feature_engineering.py`
- [ ] Add a `benford_combined_flag` feature that is `1.0` when at least two of {chi-square flag, KS flag, Kuiper flag} agree, providing a majority-vote signal

## Technical Requirements

**Benford CDF for KS test:**
- The expected Benford CDF is `F(d) = Σ_{k=1}^{d} log10(1 + 1/k)` for d = 1..9
- The observed CDF is the cumulative proportion of trades with leading digit ≤ d
- KS D-statistic: `D = max_d |F_observed(d) − F_benford(d)|`
- Critical value at α = 0.05 for one-sample KS: `D_crit = 1.358 / sqrt(N)` (Kolmogorov distribution approximation; exact for N ≥ 20)
- Set `ks_flag=True` when `D > D_crit`

**Kuiper V-statistic:**
- `V = D_plus + D_minus` where `D_plus = max_d (F_observed(d) − F_benford(d))` and `D_minus = max_d (F_benford(d) − F_observed(d))`
- Critical value approximation: `V_crit ≈ (1.747 + 0.12/sqrt(N) + 0.11/N) / (sqrt(N) + 0.155 + 0.24/sqrt(N))` (Stephens, 1970)
- Return exact p-value using the Kuiper distribution series approximation (see Press et al., *Numerical Recipes*, §14.3)
- Set `kuiper_flag=True` when p-value < 0.05

**Fallback when `astropy` is unavailable:**
- Include a self-contained `_kuiper_pvalue(V, N)` function using the series expansion: `P(V > v) ≈ 2 Σ_{j=1}^{100} (4j²v² − 1) exp(−2j²v²)` where `v = V × (sqrt(N) + 0.155 + 0.24/sqrt(N))`
- Add `astropy` to `requirements.txt` as an optional dependency with a comment: `# optional: kuiper test; fallback implemented if absent`

**Majority-vote `benford_combined_flag`:**
- Inputs: `chi2_flag_{window}`, `ks_flag_{window}`, `kuiper_flag_{window}`
- `benford_combined_flag_{window} = 1.0` if at least 2 of the 3 flags are True
- This adds 5 more features (one per window), bringing total new features to 25

**Sample size validity:**
- KS and Kuiper: valid for N ≥ 5 (document this lower bound vs. chi-square's N ≥ 30)
- Return `ks_stat=NaN, ks_pval=NaN, ks_flag=False` when N < 5 rather than raising
- Log at `DEBUG` level when KS/Kuiper are computed for windows where chi-square is invalid (N < 30); these windows will now have partial signal

**Performance:**
- KS computation (9 CDF points): O(9) = O(1), negligible overhead
- Kuiper series approximation with 100 terms: < 1 ms per call
- Total overhead of adding KS + Kuiper across 5 windows: < 5 ms per wallet

## Security Considerations
- `digit_counts` input array must be validated as length-9, non-negative integers summing to N > 0 before any division operation
- p-value computations involving exponentials must be guarded against overflow/underflow; clamp series terms below `1e-300`
- The Kuiper series approximation can return values slightly outside [0, 1] due to floating-point; clamp output to `[0.0, 1.0]`

## Testing Requirements
- Unit tests covering:
  - Perfect Benford distribution (N=1000): D-statistic ≈ 0, KS p-value ≈ 1.0, not flagged
  - Uniform digit distribution (all digits equally likely): large D, small p-value, flagged
  - N=5: valid KS result, chi-square not computed (N < 30)
  - N=4: returns NaN statistics gracefully
  - `benford_combined_flag`: 2-of-3 agreement triggers flag; 1-of-3 does not
- Integration tests covering:
  - `compute_benford_features()` returns all 20 new features populated in every window
  - Feature vector length equals `len(FEATURE_NAMES)` after adding new features
- Edge cases:
  - All trades with leading digit 1 (extreme: D_max, V_max)
  - N = exactly 5, 20, 30 (boundary conditions for each test's validity regime)
  - `astropy` not installed: falls back to self-contained Kuiper implementation without import error

## Documentation Requirements
- Update `detection/benford_engine.py` docstrings for `compute_ks_statistic()` and `compute_kuiper_statistic()` with mathematical definitions and validity bounds
- Update `detection/feature_engineering.py` `FEATURE_NAMES` with new feature names and index positions
- Extend `docs/benford_stratification.md` with a section comparing chi-square vs KS vs Kuiper sensitivity profiles and guidance on interpreting `benford_combined_flag`

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: non-parametric statistical tests, `scipy.stats`, `astropy.stats`, numerical computing
- Your approach or initial thoughts on the Kuiper V-statistic p-value approximation
- Estimated time to complete

**Ideal contributor profile:** Statistician or quantitative developer with hands-on experience implementing goodness-of-fit tests beyond chi-square; familiarity with Benford's Law applications in fraud detection is a strong plus.
