---
title: "Add Population Stability Index Tracking for All 35 Feature Dimensions"
labels: ["difficulty: advanced", "area: mlops", "type: feature"]
assignees: []
---

## Summary
`detection/drift_monitor.py` computes the Population Stability Index (PSI) for drift detection, but currently applies it as an aggregate check with a coarse threshold rather than tracking PSI as a time series for each of the 35 feature dimensions individually. Without per-feature PSI time series, it is impossible to identify *which* features are drifting, at what rate, or whether the drift is accelerating — all of which are critical for understanding whether a model retraining is needed and for diagnosing why. This issue adds per-feature PSI computation, time-series storage, heatmap export, and automated alerting when PSI > 0.20 for 3 or more features simultaneously.

## Background & Context
The `DriftMonitor` in `detection/drift_monitor.py` uses PSI to measure distributional shift between training data and production scoring data:

$$\text{PSI} = \sum_{i=1}^{n} \left( \text{current}_i - \text{training}_i \right) \times \ln\left(\frac{\text{current}_i}{\text{training}_i}\right)$$

The current implementation computes a single aggregate PSI across all features or applies it feature-by-feature but does not persist the results as a time series. The `feature_distribution_snapshots` SQLite table already stores per-wallet feature values on each scoring run (see README), providing the raw data needed for PSI computation.

The missing pieces are:
1. **Per-feature PSI computation** against the training reference distribution stored in `models/training_reference.csv`
2. **Time-series storage** of per-feature PSI values in a new `feature_psi_history` SQLite table
3. **Heatmap export** as a PNG/SVG showing PSI by (feature, date) for visual drift inspection
4. **Automated alert** when the number of features with PSI > 0.20 crosses the threshold of 3

The alert should write to the `degradation_alerts` table (from ISSUE-030) with `alert_type = "feature_drift"` and, if webhooks are configured, dispatch a `feature_drift` event.

## Objectives
- [ ] Implement `compute_per_feature_psi(reference_df, current_df, feature_names, n_bins=10) -> Dict[str, float]` in `detection/drift_monitor.py` returning a PSI value for each feature
- [ ] Create `feature_psi_history` SQLite table and implement `record_psi_snapshot(psi_dict, window_days=30)` to persist per-feature PSI values with timestamp
- [ ] Implement `export_psi_heatmap(output_path: Path, days_back: int = 90) -> Path` that generates a matplotlib heatmap with features on the Y axis, dates on the X axis, and PSI value as colour intensity (green=low drift, red=high drift)
- [ ] Integrate the PSI tracking into `cli.py retrain-check` so every retrain-check run computes, stores, and alerts on per-feature PSI

## Technical Requirements

**PSI computation (per feature):**
```python
def compute_psi_for_feature(
    reference: np.ndarray,
    current: np.ndarray,
    n_bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """Compute PSI between reference and current distributions for a single feature."""
    # Use reference distribution to define bin edges
    percentile_bins = np.percentile(reference, np.linspace(0, 100, n_bins + 1))
    percentile_bins = np.unique(percentile_bins)  # deduplicate when feature is sparse
    if len(percentile_bins) < 3:
        # Degenerate: near-constant feature; PSI = 0 (no meaningful distribution to compare)
        return 0.0
    ref_counts, _ = np.histogram(reference, bins=percentile_bins)
    cur_counts, _ = np.histogram(current, bins=percentile_bins)
    ref_pct = ref_counts / (len(reference) + epsilon)
    cur_pct = cur_counts / (len(current) + epsilon)
    # Add epsilon to avoid log(0)
    ref_pct = np.clip(ref_pct, epsilon, None)
    cur_pct = np.clip(cur_pct, epsilon, None)
    psi = np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct))
    return float(psi)
```

**`compute_per_feature_psi()` wrapper:**
- Load reference distributions from `models/training_reference.csv` (one row per training sample, columns = feature names)
- Load current distributions from the `feature_distribution_snapshots` table for the last `window_days` days
- Return `Dict[str, float]` mapping feature name → PSI value, for all 35+ features

**`feature_psi_history` table schema:**
```sql
CREATE TABLE IF NOT EXISTS feature_psi_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_name TEXT NOT NULL,
    psi_value REAL NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 30,
    n_reference_samples INTEGER,
    n_current_samples INTEGER,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_psi_history_feature ON feature_psi_history(feature_name, computed_at);
```

**PSI alert threshold and logic:**
```python
PSI_THRESHOLD: float = 0.20                  # from config/settings.py (already exists)
PSI_MIN_DRIFTED_FEATURES: int = 3            # from config/settings.py (already exists)
PSI_ALERT_COOLDOWN_HOURS: int = 24           # do not re-alert within 24 hours
```

Alert logic in `check_psi_and_alert()`:
1. Compute per-feature PSI
2. Count features with PSI > threshold
3. If count ≥ `PSI_MIN_DRIFTED_FEATURES` AND last alert > `PSI_ALERT_COOLDOWN_HOURS` ago:
   - Write alert to `degradation_alerts` with `alert_type="feature_drift"`, `affected_features=json.dumps(list)`, `n_drifted=count`
   - Dispatch webhook event if subscribers exist

**Heatmap export using matplotlib:**
```python
def export_psi_heatmap(output_path: Path, days_back: int = 90) -> Path:
    """
    Generate a (n_features × n_dates) heatmap of PSI values.
    Colour scale: 0.0=white, 0.10=yellow, 0.20=orange, 0.25+=red
    """
    df = load_psi_history(days_back=days_back)
    pivot = df.pivot(index="feature_name", columns="computed_at_date", values="psi_value")
    fig, ax = plt.subplots(figsize=(max(8, len(pivot.columns) * 0.5), max(6, len(pivot.index) * 0.3)))
    cmap = LinearSegmentedColormap.from_list("psi", ["white", "yellow", "orange", "red"], N=256)
    sns.heatmap(pivot, ax=ax, cmap=cmap, vmin=0.0, vmax=0.30, annot=False, linewidths=0.5)
    ax.set_title(f"Feature PSI Heatmap (last {days_back} days)")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    return output_path
```

Dependencies: `seaborn>=0.13.0` added to `requirements.txt` (or use pure matplotlib if seaborn is already absent).

**`GET /admin/psi-heatmap` API endpoint:**
- Returns the most recently generated PSI heatmap as a PNG file
- If no heatmap exists, generate one on demand (blocking, may take up to 5s)
- Requires admin API key

**`GET /admin/psi-history` API endpoint:**
```
GET /admin/psi-history?feature=chi2_24h&days=90
→ 200: [{"feature_name": "chi2_24h", "psi_value": 0.15, "computed_at": "...", "window_days": 30}, ...]
GET /admin/psi-history?days=30  (all features, last 30 days)
```

**`drift_reports/` JSON enhancement:**
Add `per_feature_psi` object to the drift report JSON:
```json
{
  "drift_detected": true,
  "n_drifted_features": 5,
  "psi_report": {
    "chi2_24h": 0.31,
    "wash_ring_membership": 0.08,
    ...
  },
  "drifted_features": ["chi2_24h", "counterparty_concentration_ratio", ...]
}
```

## Security Considerations
- The PSI heatmap image is an internal governance artifact; `GET /admin/psi-heatmap` requires the admin API key
- `feature_psi_history` table contains model performance metadata; protect with the same access controls as `degradation_alerts`
- Heatmap images are generated server-side using matplotlib; the filename must be constructed from a fixed pattern (not user input) to prevent path traversal

## Testing Requirements
- Unit tests covering:
  - `compute_psi_for_feature()` on identical distributions: PSI ≈ 0
  - `compute_psi_for_feature()` on highly different distributions: PSI > 0.25
  - Degenerate case: near-constant feature (all values = 1.0) → PSI = 0.0, no exception
  - Epsilon prevents log(0): verified by testing with zero-count bins
- Integration tests covering:
  - `compute_per_feature_psi()` returns a dict with all 35 feature names as keys
  - `record_psi_snapshot()` inserts 35 rows to `feature_psi_history` table
  - Alert triggered when 3+ features exceed PSI 0.20 (mock threshold)
  - `export_psi_heatmap()` creates a valid PNG file on headless system
- Edge cases:
  - No `feature_distribution_snapshots` data: `compute_per_feature_psi()` returns all-zero PSI with WARNING
  - `training_reference.csv` absent: `compute_per_feature_psi()` raises `FileNotFoundError` with clear message
  - Single feature with PSI > 0.20 (below threshold of 3): alert not triggered

## Documentation Requirements
- Update `detection/drift_monitor.py` module docstring with per-feature PSI tracking workflow
- Add `PSI_ALERT_COOLDOWN_HOURS` to `config/settings.py`
- Update `cli.py` `retrain-check` help text explaining PSI computation is now per-feature
- Update README "Continuous Retraining" section with per-feature PSI monitoring description and link to `docs/drift_monitoring.md`

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: PSI computation, drift detection, matplotlib heatmaps, SQLite time-series queries
- Your approach or initial thoughts on the bin-edge strategy for PSI computation
- Estimated time to complete

**Ideal contributor profile:** MLOps engineer with production model monitoring experience; familiarity with PSI implementation details (bin edges from reference distribution, epsilon smoothing) and matplotlib/seaborn visualisation.
