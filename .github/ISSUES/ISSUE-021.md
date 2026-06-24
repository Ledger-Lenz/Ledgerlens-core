---
title: "Extend Benford Engine to Support Multi-Asset Pair Stratified Analysis"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary
The current `benford_engine.py` computes chi-square and Z-score statistics globally across all trades for a wallet, regardless of which asset pair was traded. This conflates trading behaviour across fundamentally different markets (e.g., XLM/USDC vs. BTC/ETH), masking asset-pair-specific wash-trading patterns and producing diluted signals. Stratifying Benford analysis independently per asset-pair stratum enables targeted anomaly detection and reduces false negatives from cross-pair dilution.

## Background & Context
In `detection/benford_engine.py`, the function `compute_benford_features()` currently accepts a flat list of trade amounts and computes a single set of Benford statistics (chi-square, per-digit Z-score, MAD) over the entire sequence. The `FEATURE_NAMES` list in `detection/feature_engineering.py` reflects 15 Benford features across 5 rolling windows but does not differentiate by asset pair.

Wash-trading rings frequently concentrate on a single asset pair — e.g., a bot cycling through XLM/USDC to inflate 24-hour volume. When that ring's trades are aggregated with legitimate multi-asset trading activity, the Benford deviation signal is attenuated. A stratified approach computes the same 15 features independently for each `(wallet, asset_pair)` stratum and then derives a cross-stratum anomaly summary (max deviation, weighted average, stratum count above threshold).

The `RiskScore` schema in `detection/risk_score.py` already carries `asset_pair` as a first-class field, meaning the detection engine already operates per `(wallet, asset_pair)` pair. The Benford layer should match this granularity.

Reference: the `BenfordResult` dataclass (or equivalent return type in `benford_engine.py`) must be extended to carry `asset_pair` labelling and a `stratum_results` mapping.

## Objectives
- [ ] Refactor `compute_benford_features()` to accept a `Dict[str, List[float]]` mapping asset-pair → amounts, and return per-stratum `BenfordResult` objects alongside an aggregated summary
- [ ] Add a `stratified_benford_analysis()` top-level function that accepts a `List[Trade]` (from `ingestion/data_models.py`), groups by `asset_pair`, filters strata with fewer than 30 observations, and calls `compute_benford_features()` per stratum
- [ ] Extend `detection/feature_engineering.py` to call `stratified_benford_analysis()` and append per-stratum max-chi-square, max-MAD, and stratum-above-threshold-count features to the feature vector
- [ ] Update `FEATURE_NAMES` and the 35-feature contract in `feature_engineering.py` to document the new stratum summary features without breaking existing feature indices

## Technical Requirements

**Stratification logic:**
- Group trades by the canonical `asset_pair` string (`f"{base_asset}/{counter_asset}"` with lexicographic ordering to avoid `XLM/USDC` vs. `USDC/XLM` duplicates)
- Minimum stratum size: **N ≥ 30** trades before Benford statistics are considered valid; strata below this threshold must return `BenfordResult(valid=False, reason="insufficient_sample")`
- When all strata have N < 30, fall back gracefully to a global (unstratified) computation and set a `fallback_global=True` flag on the returned summary

**Chi-square computation:**
- Use the Pearson chi-square statistic: `χ² = Σ (observed_i − expected_i)² / expected_i` for digits 1–9
- Degrees of freedom: 8 (9 digit bins − 1)
- Reject at α = 0.05 critical value: 15.507; set `benford_flag=True` when `χ² > 15.507`
- Avoid `scipy.stats.chisquare` for the leading-digit extraction step; implement digit extraction as `int(str(abs(amount)).lstrip('0.')[0])` with input validation

**Z-score per digit:**
- `Z_d = (observed_freq_d − expected_freq_d) / sqrt(expected_freq_d × (1 − expected_freq_d) / N)`
- Flag individual digits with `|Z_d| > 1.96` (p < 0.05, two-tailed)
- Return `z_scores: Dict[int, float]` and `flagged_digits: List[int]` per stratum

**MAD aggregation:**
- `MAD = (1/9) Σ |observed_freq_d − expected_freq_d|`
- Thresholds: `MAD < 0.006` close conformity; `0.006–0.012` acceptable; `0.012–0.015` marginal; `> 0.015` non-conforming
- Cross-stratum summary feature: `max_stratum_MAD`, `mean_stratum_MAD`, `n_strata_above_0015`

**Summary feature additions (append to feature vector, do not replace):**
- `max_stratum_chi2_{window}` — highest chi-square across all valid strata in that window
- `max_stratum_MAD_{window}` — highest MAD across all valid strata
- `n_flagged_strata_{window}` — count of strata with `benford_flag=True`
- These extend the feature vector by 15 features (3 new × 5 windows), bringing total baseline from 35 → 50; update `FEATURE_NAMES` accordingly

**Performance:**
- Stratified computation for a wallet with 10 asset pairs and 1,000 trades per pair must complete in < 50 ms on a single CPU core
- Cache per-stratum digit histograms using `functools.lru_cache` keyed on `(wallet, asset_pair, window_label)`

## Security Considerations
- Asset-pair strings from Horizon API responses must be sanitised before use as dict keys or log entries; reject strings longer than 30 characters or containing characters outside `[A-Z0-9/.-]`
- The `lstrip('0.')` digit-extraction approach must handle `NaN`, `Inf`, zero, and negative amounts without raising exceptions; log and skip invalid values
- Do not store raw trade amounts in the stratum cache; cache only the digit-frequency histogram (9 integers) to minimise memory exposure of financial data

## Testing Requirements
- Unit tests covering:
  - Correct digit extraction for amounts: `0.0031` → digit `3`; `100.00` → digit `1`; `9.99` → digit `9`
  - Chi-square computation against known expected values (hand-computed fixture)
  - Stratum filtering when N < 30 returns `valid=False`
  - Lexicographic asset-pair normalisation (`USDC/XLM` → `USDC/XLM`, `XLM/USDC` → `XLM/USDC` consistently)
- Integration tests covering:
  - `stratified_benford_analysis()` on a synthetic `List[Trade]` with 3 asset pairs; verify 3 stratum results returned
  - Feature vector length after stratification equals `len(FEATURE_NAMES)`
  - Fallback-to-global path when all strata have N < 30
- Edge cases:
  - Single asset pair with exactly 30 trades (boundary condition)
  - All trades for a single amount (e.g., 100.0 — extreme Benford violation)
  - Empty trade list → returns all-zero features without exception
  - Asset-pair string with unexpected format (e.g., `"native"`) handled gracefully

## Documentation Requirements
- Update `detection/benford_engine.py` module docstring with stratification behaviour, fallback logic, and validity criteria
- Update `detection/feature_engineering.py` `FEATURE_NAMES` list with new stratum summary feature names and index positions
- Add a `docs/benford_stratification.md` explaining the rationale for stratification, the minimum-N requirement, and how cross-stratum summary features are derived

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: Python statistical computing, `scipy.stats`, Benford's Law, feature engineering for fraud detection
- Your approach or initial thoughts on the stratification grouping strategy
- Estimated time to complete

**Ideal contributor profile:** Python data scientist with experience in statistical anomaly detection, comfortable extending numerical feature pipelines without breaking existing feature index contracts.
