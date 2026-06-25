# Ensemble Stacking with Meta-Learner

LedgerLens uses a **stacking ensemble** to combine the outputs of RF, XGBoost,
and LightGBM via a logistic regression meta-learner trained on out-of-fold
(OOF) predictions. This outperforms equal-weight averaging by learning the
optimal combination from data.

## Architecture

```
Training data
      │
      ▼
 ┌────────────────────────────────────┐
 │    Walk-forward CV (5 folds)       │
 │                                    │
 │  Fold 1: train RF/XGB/LGBM        │
 │          → OOF predictions [0]     │
 │  Fold 2: ...                       │
 │  ...                               │
 └────────────────────────────────────┘
      │
      ▼
 OOF prediction matrix (n × 3)
      │
      ▼
 Logistic Regression meta-learner
      │
      ▼
 Ensemble probability → RiskScore
```

## OOF Generation

Out-of-fold (OOF) predictions prevent label leakage that would inflate
meta-learner performance:

1. Split training data into K=5 temporal folds (chronological order)
2. For each fold: train base models on the earlier K-1 folds, predict on the
   held-out fold
3. Collect all held-out predictions to form the OOF matrix

A 7-day purge gap between train and validation sets prevents temporal leakage
(wash-trading patterns evolve over days, so adjacent time windows may be
correlated).

## Why Logistic Regression as Meta-Learner?

| Property | Detail |
|----------|--------|
| Low variance | Only 3 input features — no overfitting risk |
| Calibrated outputs | Produces well-calibrated probabilities for `RiskScore.confidence` |
| Interpretable | Coefficients reveal which base model the meta-learner trusts |
| Fast | < 0.1 ms inference on 3-dimensional input |

## Optional Disagreement Features

When `STACKING_USE_DISAGREEMENT_FEATURES = True` (default), two additional
features are appended to the meta-learner input:

- `model_disagreement = max(base_proba) − min(base_proba)` — high values
  signal model uncertainty
- `oof_mean = mean(base_proba)` — the current equal-weight baseline as a feature

## Interpreting Meta-Learner Coefficients

After training, coefficients are logged at INFO level:

```
Meta-learner coefficients: rf=0.31, xgb=0.45, lgbm=0.24
Meta-learner intercept: -0.12
Meta-learner AUC-PR: 0.891 (vs. equal-weight average: 0.873)
```

A higher coefficient means the meta-learner trusts that model more. If one
model's coefficient is near zero, it may be redundant given the others.

## Fallback Behaviour

When `meta_learner.joblib` is absent, `ModelInference` falls back to
equal-weight averaging and logs:

```
INFO meta-learner not found; using equal-weight averaging
```

This ensures backward compatibility when a model directory was created before
the stacking feature was introduced.

## Model Files

| File | Description |
|------|-------------|
| `models/random_forest.joblib` | RF base model |
| `models/xgboost.joblib` | XGBoost base model |
| `models/lightgbm.joblib` | LightGBM base model |
| `models/meta_learner.joblib` | Stacking meta-learner (optional) |

The `/health` endpoint checks for all four files.

## Performance Impact

- **Training**: OOF generation costs ~3× training time (5 folds × 3 models)
- **Inference**: meta-learner `predict_proba` adds < 0.1 ms per sample
- **Expected improvement**: 2–5% AUC-PR over equal-weight averaging on clean
  synthetic data with distinct class boundaries
