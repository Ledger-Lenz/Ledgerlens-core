# Hyperparameter Optimization

## Overview

LedgerLens uses [Optuna](https://optuna.org) with the Tree-structured Parzen Estimator (TPE) algorithm for Bayesian hyperparameter optimization of the Random Forest, XGBoost, and LightGBM classifiers.

## TPE Algorithm

TPE models the probability of a hyperparameter configuration being "good" (above the median objective value) vs. "bad". Unlike random search or grid search, TPE focuses sampling on promising regions of the search space, significantly outperforming both for budgets of 50-200 trials.

## Usage

```bash
# Run 100-trial optimization before training (default)
python cli.py train --optimize

# Override trial budget
python cli.py train --optimize --n-trials 50

# Cap wall-clock time to 15 minutes per model
python cli.py train --optimize --timeout 900
```

## Search Spaces

### Random Forest
- `n_estimators`: 100-500 (step 50)
- `max_depth`: None, 5, 10, 15, 20
- `min_samples_split`: 2-20
- `min_samples_leaf`: 1-10
- `max_features`: sqrt, log2, 0.3, 0.5
- `class_weight`: balanced, balanced_subsample, None
- `bootstrap`: True, False

### XGBoost
- `n_estimators`: 100-600 (step 50)
- `max_depth`: 3-10
- `learning_rate`: 1e-3 to 0.3 (log-uniform)
- `subsample`: 0.5-1.0
- `colsample_bytree`: 0.5-1.0
- `reg_alpha`: 1e-8 to 10.0 (log-uniform)
- `reg_lambda`: 1e-8 to 10.0 (log-uniform)
- `scale_pos_weight`: 1.0-50.0
- `min_child_weight`: 1-10

### LightGBM
- `n_estimators`: 100-600
- `max_depth`: -1 to 10 (-1 = unlimited)
- `learning_rate`: 1e-3 to 0.3 (log-uniform)
- `num_leaves`: 20-150
- `min_child_samples`: 5-100
- `subsample`: 0.5-1.0
- `colsample_bytree`: 0.5-1.0
- `reg_alpha`: 1e-8 to 10.0 (log-uniform)
- `reg_lambda`: 1e-8 to 10.0 (log-uniform)
- `is_unbalance`: True, False

## Cross-Validation

Optimization uses `TimeSeriesSplit(n_splits=3, gap=100)` to prevent data leakage. The 100-sample purge gap ensures no validation sample's feature window overlaps training data.

The objective function maximises mean AUC-PR (Average Precision) across CV folds, which is more sensitive than AUC-ROC for the imbalanced wash-trade detection task.

## Pruning

`MedianPruner(n_startup_trials=5, n_warmup_steps=2)` terminates unpromising trials early based on intermediate AUC-PR values reported after each CV fold, reducing effective compute budget.

## Persistence

- **Optuna studies**: `models/optuna_studies/{model_name}.db` (SQLite, gitignored)
- **Best parameters**: `models/best_hyperparams.json`

## Inspecting Study Results

```python
import optuna
study = optuna.load_study(
    study_name="random_forest_<hash>",
    storage="sqlite:///models/optuna_studies/random_forest.db",
)
print(study.best_params)
print(study.best_value)
optuna.visualization.plot_optimization_history(study)
```
