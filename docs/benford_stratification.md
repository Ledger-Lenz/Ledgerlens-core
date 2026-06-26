# Benford Stratification

## Rationale

Wash-trading rings frequently concentrate on a single asset pair (e.g. a bot cycling through XLM/USDC to inflate 24-hour volume). When that ring's trades are aggregated with legitimate multi-asset trading activity, the Benford deviation signal is attenuated. Stratifying Benford analysis independently per `(wallet, asset_pair)` stratum enables targeted anomaly detection and reduces false negatives from cross-pair dilution.

## Asset-Pair Normalisation

Asset pairs are canonicalised by lexicographic ordering of the two asset symbols. This avoids treating `XLM/USDC` and `USDC/XLM` as distinct strata:

```
canonical_pair = "/".join(sorted([base_symbol, counter_symbol]))
```

Asset-pair strings are sanitised: strings longer than 30 characters or containing characters outside `[A-Z0-9/.\-:]` are rejected and their trades are excluded from stratified analysis.

## Minimum-N Requirement

A stratum must have N >= 30 valid trades before Benford statistics (chi-square, Z-scores, MAD) are computed. Strata below this threshold return `BenfordResult(valid=False, reason="insufficient_sample")`.

When **all** strata in a window have N < 30, the engine falls back to a global (unstratified) computation and sets `fallback_global=True` on the returned `StratifiedBenfordSummary`.

## Statistical Tests

Per stratum, three tests are computed:

| Test | Statistic | Flag threshold |
|------|-----------|----------------|
| Pearson chi-square | chi2 = sum((O_i - E_i)^2 / E_i), df=8 | chi2 > 15.507 (alpha=0.05) |
| Per-digit Z-score | Z_d = (obs_d - exp_d) / sqrt(exp_d * (1 - exp_d) / N) | abs(Z_d) > 1.96 |
| MAD | (1/9) * sum(abs(obs_d - exp_d)) | > 0.015 = non-conforming |

MAD conformity thresholds:
- < 0.006: close conformity
- 0.006-0.012: acceptable
- 0.012-0.015: marginal
- > 0.015: non-conforming

## Cross-Stratum Summary Features

Three summary features are derived per rolling window and appended to the feature vector:

| Feature | Description |
|---------|-------------|
| `max_stratum_chi2_{window}` | Highest chi-square across all valid strata |
| `max_stratum_MAD_{window}` | Highest MAD across all valid strata |
| `n_flagged_strata_{window}` | Count of strata where `benford_flag=True` |

These 15 features (3 per window x 5 windows) extend the feature vector without altering existing feature indices.

## Chi-Square vs KS vs Kuiper Sensitivity Profiles

Three complementary tests are computed per window to cover different failure modes:

| Test | Validity | Strength | Weakness |
|------|----------|----------|----------|
| **Chi-square** | N >= 30 (expected cell counts >= 5) | Sensitive to overall distributional differences | Loses power for small N; asymptotic approximation breaks down |
| **KS (Kolmogorov-Smirnov)** | N >= 5 | Exact for finite N; no minimum cell count needed | Less sensitive to local deviations at distribution tails |
| **Kuiper** | N >= 5 | Rotation-invariant; more sensitive to tail deviations (digits 1, 9) | Slightly lower power than KS for central deviations |

The KS and Kuiper tests are particularly valuable in the 1h and 4h windows where N is often below 50, making chi-square unreliable. Wash-trading bots using round lot sizes (100, 1000) tend to deviate at the tails (overrepresentation of digit 1), which Kuiper detects more reliably than KS.

### KS Test Details

- D-statistic: `D = max_d |F_observed(d) - F_benford(d)|`
- Critical value (alpha=0.05): `D_crit = 1.358 / sqrt(N)`
- Flagged when `D > D_crit`

### Kuiper Test Details

- V-statistic: `V = D_plus + D_minus` where `D_plus = max(F_obs - F_benford)`, `D_minus = max(F_benford - F_obs)`
- P-value via series approximation (Press et al., Numerical Recipes, ch. 14.3)
- Flagged when `p-value < 0.05`
- Self-contained fallback implementation included when `astropy` is unavailable

## Interpreting `benford_combined_flag`

The `benford_combined_flag_{window}` feature is a majority-vote signal: it equals 1.0 when at least 2 of the 3 tests (chi-square, KS, Kuiper) flag the distribution as non-Benford. This reduces false positives from any single test's idiosyncrasies while maintaining sensitivity.

| Flags agreeing | `benford_combined_flag` | Interpretation |
|:-:|:-:|---|
| 0 | 0.0 | No evidence of non-Benford behaviour |
| 1 | 0.0 | Weak signal; single-test anomaly, likely noise |
| 2 | 1.0 | Moderate evidence; two independent tests agree |
| 3 | 1.0 | Strong evidence; all tests flag non-conformity |
