# Hyperparameter Optimization with Optuna

## Overview

LedgerLens uses [Optuna](https://optuna.org)'s Tree-structured Parzen
Estimator (TPE) for Bayesian hyperparameter optimization.  TPE models the
probability of trial configurations being good, significantly outperforming
random and grid search for budgets of 50–200 trials.

## Usage

```bash
# Run optimization with default 100 trials per model
python -m cli train --optimize

# Custom trial budget and timeout
python -m cli train --optimize --n-trials 50 --timeout 900
```

## Search Spaces

### Random Forest
- `n_estimators`: 100–500 (step 50)
- `max_depth`: None, 5, 10, 15, 20
- `min_samples_split`: 2–20
- `min_samples_leaf`: 1–10
- `max_features`: sqrt, log2, 0.3, 0.5
- `class_weight`: balanced, balanced_subsample, None
- `bootstrap`: True, False

### XGBoost
- `n_estimators`: 100–600 (step 50)
- `max_depth`: 3–10
- `learning_rate`: 1e-3 – 0.3 (log-uniform)
- `subsample`: 0.5–1.0
- `colsample_bytree`: 0.5–1.0
- `reg_alpha`, `reg_lambda`: 1e-8 – 10.0 (log-uniform)
- `scale_pos_weight`: 1.0–50.0
- `min_child_weight`: 1–10

### LightGBM
- `n_estimators`: 100–600
- `max_depth`: -1 (unlimited) to 10
- `learning_rate`: 1e-3 – 0.3 (log-uniform)
- `num_leaves`: 20–150
- `min_child_samples`: 5–100
- `subsample`, `colsample_bytree`: 0.5–1.0
- `reg_alpha`, `reg_lambda`: 1e-8 – 10.0 (log-uniform)
- `is_unbalance`: True, False

## Cross-Validation

Optimization uses `TimeSeriesSplit(n_splits=3, gap=100)` to prevent data
leakage during hyperparameter search.  Each trial's objective is the mean
AUC-PR across folds.  `MedianPruner` terminates unpromising trials early.

## Inspecting Results

Studies are persisted as SQLite databases in `models/optuna_studies/`.
Best parameters are written to `models/best_hyperparams.json`.

```python
import optuna
study = optuna.load_study(
    study_name="random_forest_<hash>",
    storage="sqlite:///models/optuna_studies/random_forest.db",
)
print(study.best_params)
print(study.best_value)

# Visualize optimization history
optuna.visualization.plot_optimization_history(study)
```

## Performance

100 trials × 3-fold CV × 3 models ≈ 900 model fits.  Target: < 30 minutes
on a 4-core machine.  Parallel execution via `n_jobs=-1`.

## Safety Bounds

- `n_trials` is capped at 1000
- `timeout_seconds` is capped at 86400 (24 hours)
- Study databases are excluded from git (`.gitignore`)
