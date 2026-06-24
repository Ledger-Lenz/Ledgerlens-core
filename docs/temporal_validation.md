# Temporal Validation Strategy

## Data Leakage in Financial ML

Random train/test splits are inappropriate for time-series financial data.
When features use rolling windows (e.g. 7-day Benford statistics), a
randomly-selected validation sample may have its feature window overlap with
training samples, causing the model to see raw trade data underlying
validation features during training.  This produces optimistically biased
evaluation metrics; real-world performance is typically 10–30 % lower
(Lopez de Prado, *Advances in Financial Machine Learning*, 2018).

## Purge Gap Strategy

LedgerLens uses `temporal_train_val_split()` which:

1. **Sorts** all samples by their trade timestamp.
2. **Splits** at a chronological cutoff (default: earliest 80 % → train).
3. **Purges** samples whose feature window could overlap the boundary:
   - Samples between `cutoff − max_window_days` and `cutoff + gap_days`
     are excluded from both sets.
   - `gap_days` (default 7) and `max_window_days` (default 30) are
     configurable via `config/settings.py`.

```
 ──────────────────────────────────────────────────────────────────►  time
 [ ─────── TRAIN ─────── ][ purge gap ][ ──── VALIDATION ──── ]
                          ◄─ max_window ─►◄─ gap_days ─►
```

## Walk-Forward Cross-Validation

`walk_forward_cv()` implements rolling-origin validation:

```
Fold 1:  [TRAIN          ]  gap  [VAL   ]
Fold 2:  [TRAIN                 ]  gap  [VAL   ]
Fold 3:  [TRAIN                        ]  gap  [VAL   ]
```

Each fold uses an expanding training window with a configurable purge gap
before the validation window.  This mirrors production conditions where the
model is retrained on all historical data before scoring new observations.

## Pipeline Order

The training pipeline enforces a strict order to prevent leakage:

1. **Temporal split** — chronological train/val separation with purge gap
2. **Oversample** (SMOTE/ADASYN) — applied to training data **only**
3. **Fit** — train models on the oversampled training set
4. **Evaluate** — score on the untouched validation set

## Data Leakage Audit

`data_leakage_audit()` checks that no validation sample's feature window
overlaps with any training sample's timestamp.  It raises `DataLeakageError`
if overlap is detected.  This should be called in CI/test environments
whenever a train/val split is created.

## Configuration

| Setting                          | Default | Description                              |
|----------------------------------|---------|------------------------------------------|
| `TEMPORAL_SPLIT_VAL_RATIO`       | 0.20    | Fraction of data reserved for validation |
| `TEMPORAL_SPLIT_GAP_DAYS`        | 7.0     | Days of purge gap after cutoff           |
| `TEMPORAL_SPLIT_MAX_WINDOW_DAYS` | 30.0    | Maximum feature look-back window (days)  |
| `WALK_FORWARD_N_SPLITS`          | 5       | Number of walk-forward CV folds          |
