---
title: "Implement Temporal Train/Validation Split to Prevent Leakage in Model Evaluation"
labels: ["difficulty: advanced", "area: ml", "type: correctness"]
assignees: []
---

## Summary
`detection/model_training.py` and `detection/dataset.py` currently use random train/test splits (`sklearn.model_selection.train_test_split`), which is inappropriate for time-series financial data. Random splitting allows future trade observations to appear in the training set and past observations in the validation set, causing data leakage that inflates reported AUC-ROC scores and produces models that underperform in production. Replacing random splits with strict time-based splits — with a purge gap between training and validation windows — is essential for honest evaluation and reliable model selection.

## Background & Context
In `detection/dataset.py`, the `build_dataset()` function assembles feature vectors from historical trades and assigns wash-trading labels. The current split in `model_training.py` shuffles the dataset randomly before splitting:

```python
X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42)
```

This is a well-known source of data leakage in financial ML (Lopez de Prado, *Advances in Financial Machine Learning*, 2018):
- Feature windows (e.g., 7d rolling Benford statistics) for a validation sample computed on 2024-06-20 include data from 2024-06-13 through 2024-06-20
- If any training samples were drawn from 2024-06-14–2024-06-20, the model has seen the raw trade data underlying validation features during training
- This produces optimistically biased evaluation metrics; real-world performance is typically 10–30% lower

The fix: sort all samples by `timestamp`, use the earliest 70% for training, add a **purge gap** (skip samples whose feature window overlaps with any training sample), and use the remainder for validation. For walk-forward (rolling-origin) validation, implement `TimeSeriesSplit` with a configurable gap parameter.

## Objectives
- [ ] Implement `temporal_train_val_split(X, y, timestamps, val_ratio=0.2, gap_days=7) -> Tuple[...]` in `detection/dataset.py` that sorts by timestamp, separates train/val by chronological cutoff, and excludes gap samples
- [ ] Implement `walk_forward_cv(X, y, timestamps, n_splits=5, gap_days=7) -> Generator[...]` for cross-validation yielding (train_idx, val_idx) tuples respecting chronological order and gaps
- [ ] Replace all `train_test_split` calls in `model_training.py` with `temporal_train_val_split`; ensure the oversampler (SMOTE/ADASYN) is applied only to the training portion
- [ ] Add a `data_leakage_audit()` function that checks whether any validation sample's feature window overlaps with any training sample's timestamp, and raises `DataLeakageError` if overlap is detected

## Technical Requirements

**`temporal_train_val_split` implementation:**
```python
def temporal_train_val_split(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray,  # Unix epoch float, one per sample
    val_ratio: float = 0.20,
    gap_days: float = 7.0,
    max_window_days: float = 30.0,  # maximum feature window used (for purge calculation)
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    sort_idx = np.argsort(timestamps)
    X, y, timestamps = X[sort_idx], y[sort_idx], timestamps[sort_idx]
    cutoff_ts = timestamps[int(len(timestamps) * (1 - val_ratio))]
    purge_end_ts = cutoff_ts + gap_days * 86400
    # purge gap: skip samples with timestamp in [cutoff_ts - max_window_days*86400, purge_end_ts]
    purge_start_ts = cutoff_ts - max_window_days * 86400
    train_mask = timestamps < purge_start_ts
    val_mask = timestamps >= purge_end_ts
    return X[train_mask], X[val_mask], y[train_mask], y[val_mask]
```

**Purge gap rationale:**
- The 7-day gap ensures no validation sample's feature window (max 30d) was computed using training data at the boundary
- `gap_days` should equal `max_window_days` in the strictest interpretation (Lopez de Prado) but 7 days is a practical compromise
- Make both `gap_days` and `max_window_days` configurable via `config/settings.py`

**Walk-forward CV:**
```python
def walk_forward_cv(X, y, timestamps, n_splits=5, gap_days=7, min_train_days=60):
    """Yields (train_indices, val_indices) for walk-forward validation."""
    sort_idx = np.argsort(timestamps)
    X, y, ts = X[sort_idx], y[sort_idx], timestamps[sort_idx]
    fold_duration = (ts[-1] - ts[0]) / (n_splits + 1)
    for i in range(1, n_splits + 1):
        train_end = ts[0] + fold_duration * i
        val_start = train_end + gap_days * 86400
        val_end = val_start + fold_duration
        train_idx = sort_idx[ts < train_end]
        val_idx = sort_idx[(ts >= val_start) & (ts < val_end)]
        if len(train_idx) > 0 and len(val_idx) > 0:
            yield train_idx, val_idx
```

**`DataLeakageError` audit:**
```python
class DataLeakageError(Exception):
    pass

def data_leakage_audit(train_timestamps, val_timestamps, max_window_seconds):
    """Raises DataLeakageError if any val sample's window overlaps train data."""
    val_window_start = val_timestamps.min() - max_window_seconds
    overlap = val_window_start < train_timestamps.max()
    if overlap:
        raise DataLeakageError(
            f"Leakage detected: earliest val feature window ({val_window_start:.0f}) "
            f"overlaps train data (latest: {train_timestamps.max():.0f})"
        )
```

**Integration with oversampling:**
- SMOTE/ADASYN must be applied **after** the temporal split, on `X_train, y_train` only
- The pipeline order must be: split → oversample(train only) → fit → evaluate(val, untouched)
- Document this order explicitly in `model_training.py` with an inline comment block

**`dataset.py` changes:**
- Add `timestamp` column to the feature matrix `DataFrame` (not as a feature — as metadata); propagate through to `temporal_train_val_split`
- The `Trade.timestamp` (from `ingestion/data_models.py`) is the natural source; use the **latest** trade timestamp within a wallet's feature window as the sample timestamp

**Configuration (add to `config/settings.py`):**
```python
TEMPORAL_SPLIT_VAL_RATIO: float = 0.20
TEMPORAL_SPLIT_GAP_DAYS: float = 7.0
TEMPORAL_SPLIT_MAX_WINDOW_DAYS: float = 30.0
WALK_FORWARD_N_SPLITS: int = 5
```

## Security Considerations
- `data_leakage_audit()` must be called automatically in CI/test environments whenever `model_training.py` builds a train/val split; add it to the test suite as an assertion
- The timestamp column must be excluded from the feature matrix `X` before training; add an assertion `assert "timestamp" not in feature_names` in `train_models()`
- Audit logs showing train/val split boundaries and purge gap sizes must be written to `models/training_metadata.json` for reproducibility

## Testing Requirements
- Unit tests covering:
  - `temporal_train_val_split`: val set contains only timestamps ≥ train max + gap_days×86400
  - `temporal_train_val_split`: with val_ratio=0.2 on 1000 samples, roughly 200 samples in val (accounting for purge gap)
  - `data_leakage_audit`: raises `DataLeakageError` when deliberately overlapping splits are passed
  - `data_leakage_audit`: does not raise on correct temporal split
- Integration tests covering:
  - Full `train_models()` run uses temporal split; reported AUC-ROC is reproducible (fixed random state)
  - `walk_forward_cv` yields exactly `n_splits` non-empty (train_idx, val_idx) pairs on a 365-day synthetic dataset
  - SMOTE applied to `X_train` does not modify `X_val` (assert shapes before and after)
- Edge cases:
  - Dataset with all samples within a 1-day window: `temporal_train_val_split` returns empty val set; `train_models()` logs WARNING and falls back to random split
  - Gap days larger than the entire dataset span: val set is empty; handled gracefully
  - Single sample per timestamp: splits work correctly

## Documentation Requirements
- Update `detection/dataset.py` module docstring explaining the timestamp metadata contract
- Update `detection/model_training.py` with an explicit comment block documenting the split → oversample → fit → evaluate pipeline order
- Add a `docs/temporal_validation.md` explaining data leakage in financial ML, the purge gap strategy, and walk-forward CV with diagrams

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: financial ML, time-series cross-validation, data leakage prevention, Lopez de Prado methodology
- Your approach or initial thoughts on the purge gap size trade-offs
- Estimated time to complete

**Ideal contributor profile:** ML engineer with financial domain experience and understanding of why random splits are invalid for time-series financial data; familiarity with `Advances in Financial Machine Learning` (Lopez de Prado) methodology is a strong plus.
