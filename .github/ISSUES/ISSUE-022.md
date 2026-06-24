---
title: "Implement Adaptive Benford Window Sizing Based on Trade Volume Density"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary
The current `benford_engine.py` uses fixed rolling windows (1h, 4h, 24h, 7d, 30d) regardless of how many trades actually occurred within those windows. For low-liquidity wallets or quiet market periods, the 1h and 4h windows may contain fewer than 30 trades, making chi-square and MAD statistics statistically unreliable. Adaptive window sizing automatically expands or contracts each window until the sample count N ≥ 30 condition is satisfied, ensuring that every Benford metric returned is statistically valid.

## Background & Context
Benford's Law analysis requires a minimum sample size to produce reliable chi-square statistics. The chi-square test assumes expected cell counts ≥ 5 per digit bin; with 9 bins and expected frequencies between 4.6% and 30.1%, a minimum of N ≈ 30 trades is generally accepted in the academic literature as the lower validity bound (Morrow, 2014; Nigrini, 2012).

In `detection/benford_engine.py`, the `compute_benford_features()` function is called from `detection/feature_engineering.py` with pre-sliced trade lists corresponding to fixed window labels. The window slicing currently happens in the caller without any sample-count check, so windows with N < 30 silently produce invalid statistics that flow into the ML feature vector.

The fix is a two-part adaptive strategy:
1. **Expand**: if a candidate window has N < 30, double its width up to a maximum of 90 days, stopping when N ≥ 30 or max width is reached
2. **Merge**: if no single expanded window reaches N ≥ 30, merge the two smallest adjacent windows and mark the result with `merged=True`

The `BenfordWindowResult` return type must carry the effective window width (in seconds) and a `valid: bool` flag so downstream consumers (feature engineering, API) can distinguish reliable from unreliable statistics.

## Objectives
- [ ] Implement an `AdaptiveBenfordWindow` class in `benford_engine.py` that encapsulates the expansion/merge strategy and exposes a `fit(trades: List[Trade], target_window_label: str) -> BenfordWindowResult` method
- [ ] Add a `min_sample_count: int = 30` and `max_window_days: int = 90` configuration parameter (read from `config/settings.py`) to the adaptive window logic
- [ ] Modify `compute_benford_features()` to accept an `AdaptiveBenfordWindow` instance and use it when slicing trades for each window, logging a `WARNING` when a window had to be expanded or merged
- [ ] Expose effective window widths as metadata in the `BenfordResult` so `feature_engineering.py` can set a `benford_window_expanded_{label}` boolean feature flag for each of the 5 windows

## Technical Requirements

**Adaptive expansion algorithm:**
```
for each target_window in [1h, 4h, 24h, 7d, 30d]:
    width = target_window
    while count(trades in width) < MIN_N and width < MAX_WINDOW:
        width = min(width * 2, MAX_WINDOW)
    if count(trades in width) >= MIN_N:
        return BenfordWindowResult(trades=slice(trades, width), effective_width=width, valid=True, expanded=(width > target_window))
    else:
        return BenfordWindowResult(trades=[], valid=False, reason="insufficient_even_after_expansion")
```

**Merge strategy (fallback when expansion fails):**
- Sort valid BenfordWindowResults by effective_width ascending
- Merge the two smallest if neither individually reaches MIN_N
- Merged result carries `merged=True` and `merged_windows: List[str]` for traceability
- Maximum one merge per feature computation cycle to avoid collapsing all windows into one

**Configuration (add to `config/settings.py`):**
```python
BENFORD_MIN_SAMPLE_COUNT: int = 30       # Minimum trades per window for valid chi-square
BENFORD_MAX_WINDOW_DAYS: int = 90        # Maximum window width for adaptive expansion
BENFORD_EXPANSION_FACTOR: float = 2.0   # Multiplier per expansion step
```

**Window label contract:**
- After adaptive expansion, the feature names remain `chi2_1h`, `chi2_4h`, etc. — the label reflects the *target* window, not the effective width
- Effective width is carried in `BenfordWindowResult.effective_seconds: int` for metadata/logging only
- Boolean flag features `benford_window_expanded_1h` through `benford_window_expanded_30d` (5 new features) indicate whether expansion occurred; add to `FEATURE_NAMES`

**Performance targets:**
- Adaptive window computation for 5 windows over 10,000 trades must complete in < 100 ms
- Pre-sort trades by timestamp once on entry; use `bisect` for O(log N) boundary slicing rather than linear scans
- Cache sorted trade timestamp arrays per wallet to avoid redundant sorts within a pipeline run

**Logging:**
- Log at `DEBUG` level when window is used as-is (N ≥ 30 met immediately)
- Log at `WARNING` level when expansion was necessary, including: wallet, window_label, original_N, final_N, effective_width_hours
- Log at `ERROR` level when even max-width expansion fails to reach N ≥ 30; this is a data-quality signal

## Security Considerations
- `MAX_WINDOW_DAYS` must be enforced as an absolute upper bound; do not allow configuration values above 365 days to prevent memory exhaustion from loading excessive trade history
- The expansion loop must be bounded; use a `max_iterations = ceil(log2(MAX_WINDOW_DAYS / min_window_hours))` guard to prevent infinite loops on malformed input
- Trade list slicing must not mutate the original list; use immutable slices or copies

## Testing Requirements
- Unit tests covering:
  - Window with N ≥ 30 from the start: no expansion, `expanded=False`
  - Window with N = 15 expands once to 2× width, reaches N = 35: `expanded=True`, `effective_seconds = 2 × original`
  - Window never reaches N ≥ 30 even at max width: `valid=False`
  - Merge fallback: two windows each with N = 20 merge to N = 40, `merged=True`
- Integration tests covering:
  - Full `compute_benford_features()` call with a sparse trade list (5 trades total) produces `valid=False` features without raising
  - Feature vector length remains consistent regardless of which windows expanded
- Edge cases:
  - Exactly 30 trades: valid, no expansion
  - 29 trades: triggers expansion
  - 0 trades: returns all-invalid results gracefully
  - Single trade repeated 50 times (same amount): valid N, extreme chi-square

## Documentation Requirements
- Update `detection/benford_engine.py` module docstring with the adaptive window algorithm, configuration parameters, and validity contract
- Update `config/settings.py` with inline comments for the three new Benford configuration keys
- Add a section to `docs/benford_stratification.md` (created in ISSUE-021) describing the adaptive window sizing rationale and fallback behaviour

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: Python statistical computing, rolling window algorithms, `bisect` module, time-series data handling
- Your approach or initial thoughts on the expansion strategy
- Estimated time to complete

**Ideal contributor profile:** Python engineer comfortable with statistical validity constraints and rolling time-series windowing; familiarity with Benford's Law minimum-sample requirements is a strong plus.
