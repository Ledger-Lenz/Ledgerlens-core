# Benford Analysis: Stratification & Statistical Tests

## Chi-Square vs KS vs Kuiper Sensitivity Profiles

| Test        | Min N | Strengths                                      | Weaknesses                              |
|-------------|-------|-------------------------------------------------|-----------------------------------------|
| Chi-square  | 30    | Sensitive to overall distributional differences  | Breaks down for sparse bins (N < 30)    |
| KS          | 5     | Exact for finite N, no minimum cell counts       | Less sensitive to tail deviations       |
| Kuiper      | 5     | Rotation-invariant, sensitive at tails (1 & 9)   | Slightly less power for global shifts   |

### When to use each

- **N >= 30**: All three tests are valid; use `benford_combined_flag` (majority vote)
- **5 <= N < 30**: Only KS and Kuiper are valid; chi-square is unreliable
- **N < 5**: No test is reliable; features return NaN/0

## Combined Flag (`benford_combined_flag`)

The `benford_combined_flag_{window}` feature is 1.0 when **at least 2 of 3**
tests (chi-square, KS, Kuiper) flag the distribution as non-Benford. This
majority-vote approach reduces false positives from any single test while
maintaining sensitivity.

## Asset-Pair Stratification

### Rationale

Wash-trading rings frequently concentrate on a single asset pair. When
aggregated with legitimate multi-asset trading activity, the Benford
deviation signal is attenuated. Stratified analysis computes Benford
features independently per `(wallet, asset_pair)` stratum.

### Minimum-N Requirement

Each stratum requires **N >= 30** trades for chi-square validity.
Strata below this threshold return `valid=False`. When all strata
have N < 30, the engine falls back to a global (unstratified) computation.

### Cross-Stratum Summary Features

| Feature                         | Description                                     |
|---------------------------------|-------------------------------------------------|
| `max_stratum_chi2_{window}`     | Highest chi-square across valid strata           |
| `max_stratum_MAD_{window}`      | Highest MAD across valid strata                  |
| `n_flagged_strata_{window}`     | Count of strata with `benford_flag=True`         |

These features surface the worst-case stratum signal without dilution from
well-behaved pairs.

### Asset-Pair Normalization

Pairs are canonicalized with lexicographic ordering to avoid duplicates:
`XLM/USDC` and `USDC/XLM` both resolve to `USDC/XLM`. Pair strings are
validated against `[A-Za-z0-9/.\-:]` and capped at 30 characters.

### Fallback Logic

When **all** strata have N < 30:

1. Global (unstratified) Benford metrics are computed as a fallback
2. `fallback_global=True` is set on the result
3. Stratum summary features default to 0.0

### Implementation

`stratified_benford_analysis()` in `detection/benford_engine.py` accepts
either a list of `Trade` objects or a DataFrame.  It groups trades by
canonical asset pair, filters strata below the minimum size, computes
`compute_benford_metrics()` per valid stratum, and returns per-stratum
results alongside the cross-stratum summary.
