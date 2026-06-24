---
title: "Add Bayesian Hyperparameter Optimization for All Three Ensemble Models"
labels: ["difficulty: advanced", "area: ml", "type: enhancement"]
assignees: []
---

## Summary
`detection/model_training.py` currently trains the Random Forest, XGBoost, and LightGBM classifiers with manually tuned default hyperparameters. Manual tuning is suboptimal and brittle: the best hyperparameters depend on the training data distribution, which shifts as wash-trading strategies evolve. Bayesian hyperparameter optimization with Optuna's TPE sampler automatically finds near-optimal configurations within a 100-trial budget, using temporal cross-validation to prevent data leakage, and logs the best parameters for reproducibility and auditability.

## Background & Context
The ensemble models in `detection/model_training.py` are constructed with fixed hyperparameters (e.g., `RandomForestClassifier(n_estimators=100, max_depth=None)`). These defaults are neither optimal for the LedgerLens feature set nor stable across retraining runs triggered by concept drift (see `detection/drift_monitor.py`).

Optuna (https://optuna.org) is a hyperparameter optimization framework that uses the Tree-structured Parzen Estimator (TPE) algorithm — a Bayesian method that models the probability of trial configurations being good, rather than sampling randomly. TPE significantly outperforms random search and grid search for budgets of 50–200 trials.

Key design decisions:
- **Temporal CV**: use a `TimeSeriesSplit` (sklearn) or custom walk-forward split to avoid leakage when evaluating trial hyperparameters (see ISSUE-027)
- **Pruning**: use `optuna.pruners.MedianPruner` to terminate unpromising trials early, reducing the effective compute budget
- **Objective**: maximise mean AUC-PR across CV folds (more sensitive than AUC-ROC for imbalanced wash-trade detection)
- **Persistence**: store the completed `optuna.Study` as a SQLite database in `models/optuna_studies/` so trials can be resumed and inspected

## Objectives
- [ ] Add `optimize_hyperparameters(model_name: str, X_train, y_train, n_trials: int = 100, timeout_seconds: int = 1800) -> Dict[str, Any]` to `model_training.py`
- [ ] Define search spaces for Random Forest, XGBoost, and LightGBM covering the 8–12 most impactful hyperparameters per model (specified below)
- [ ] Integrate `optimize_hyperparameters()` into the `train_models()` flow as an optional step enabled by `--optimize` CLI flag; log best params and AUC-PR improvement over defaults
- [ ] Persist Optuna studies to `models/optuna_studies/{model_name}.db` and best params to `models/best_hyperparams.json` for audit and reproducibility

## Technical Requirements

**Optuna configuration:**
```python
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)  # suppress per-trial stdout

study = optuna.create_study(
    direction="maximize",
    sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=10),
    pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=2),
    storage=f"sqlite:///models/optuna_studies/{model_name}.db",
    study_name=f"{model_name}_{version_hash}",
    load_if_exists=True,
)
study.optimize(objective, n_trials=n_trials, timeout=timeout_seconds)
```

**Random Forest search space:**
```python
n_estimators: int in [100, 500]          # step 50
max_depth: int | None — categorical [None, 5, 10, 15, 20]
min_samples_split: int in [2, 20]
min_samples_leaf: int in [1, 10]
max_features: categorical ["sqrt", "log2", 0.3, 0.5]
class_weight: categorical ["balanced", "balanced_subsample", None]
bootstrap: categorical [True, False]
```

**XGBoost search space:**
```python
n_estimators: int in [100, 600]          # step 50
max_depth: int in [3, 10]
learning_rate: float log-uniform in [1e-3, 0.3]
subsample: float in [0.5, 1.0]
colsample_bytree: float in [0.5, 1.0]
reg_alpha: float log-uniform in [1e-8, 10.0]
reg_lambda: float log-uniform in [1e-8, 10.0]
scale_pos_weight: float in [1.0, 50.0]  # handles imbalance natively
min_child_weight: int in [1, 10]
```

**LightGBM search space:**
```python
n_estimators: int in [100, 600]
max_depth: int in [-1, 10]  # -1 = unlimited
learning_rate: float log-uniform in [1e-3, 0.3]
num_leaves: int in [20, 150]
min_child_samples: int in [5, 100]
subsample: float in [0.5, 1.0]
colsample_bytree: float in [0.5, 1.0]
reg_alpha: float log-uniform in [1e-8, 10.0]
reg_lambda: float log-uniform in [1e-8, 10.0]
is_unbalance: categorical [True, False]
```

**Objective function:**
```python
def objective(trial):
    params = suggest_params(trial, model_name)
    model = build_model(model_name, params)
    tscv = TimeSeriesSplit(n_splits=3, gap=100)  # 100-sample purge gap
    aucs = []
    for train_idx, val_idx in tscv.split(X_train):
        model.fit(X_train[train_idx], y_train[train_idx])
        proba = model.predict_proba(X_train[val_idx])[:, 1]
        aucs.append(average_precision_score(y_train[val_idx], proba))
        trial.report(np.mean(aucs), step=len(aucs))
        if trial.should_prune():
            raise optuna.TrialPruned()
    return np.mean(aucs)
```

**CLI flag:**
- `python cli.py train --optimize` — run 100-trial Optuna optimization before final training
- `python cli.py train --optimize --n-trials 50` — override trial budget
- `python cli.py train --optimize --timeout 900` — cap wall-clock time to 15 minutes

**Best params persistence:**
```json
{
  "random_forest": {"n_estimators": 350, "max_depth": 10, ...},
  "xgboost": {"learning_rate": 0.05, "max_depth": 6, ...},
  "lightgbm": {"num_leaves": 63, "learning_rate": 0.02, ...},
  "optimization_date": "2026-06-24T09:25:23Z",
  "n_trials_completed": {"random_forest": 100, "xgboost": 87, ...},
  "best_auc_pr": {"random_forest": 0.842, "xgboost": 0.891, "lightgbm": 0.876}
}
```

**Performance:**
- 100 trials × 3-fold CV × 3 models ≈ 900 model fits; target < 30 minutes on a 4-core machine
- Parallelise with `study.optimize(..., n_jobs=-1)` when multiple cores are available

## Security Considerations
- Optuna SQLite study databases contain trial parameters and AUC-PR scores; they must not be committed to git (add `models/optuna_studies/` to `.gitignore`)
- The `study_name` must include a version hash (not a timestamp) to ensure that resumed studies correspond to the same training data; using wall-clock timestamps as study names allows stale studies to be resumed with mismatched data
- `n_trials` and `timeout_seconds` must be bounded: reject values > 1000 and > 86400 respectively to prevent runaway optimization jobs

## Testing Requirements
- Unit tests covering:
  - `suggest_params(trial, "random_forest")` returns a dict with all expected keys
  - `suggest_params(trial, "xgboost")` covers log-uniform params correctly
  - `optimize_hyperparameters()` with `n_trials=2` completes without error and returns a dict
- Integration tests covering:
  - Full optimization run on synthetic data (200 samples, 10% positive): 5 trials completes in < 60s
  - Best params written to `models/best_hyperparams.json` with all required fields
  - Optuna study persisted to SQLite and loadable (`load_if_exists=True`)
- Edge cases:
  - All trials pruned: `optimize_hyperparameters()` returns default params and logs WARNING
  - `n_trials=1`: single-trial run with no pruning
  - Timeout reached before all trials complete: returns best params found so far

## Documentation Requirements
- Update `detection/model_training.py` module docstring with the optimization workflow and CLI flag
- Add `optuna` to `requirements.txt` (pinned version, e.g., `optuna==3.6.0`)
- Update `cli.py` help text with `--optimize`, `--n-trials`, `--timeout` flags
- Add a `docs/hyperparameter_optimization.md` explaining TPE algorithm, search spaces, and how to inspect Optuna study results

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: Optuna, Bayesian hyperparameter optimization, XGBoost/LightGBM tuning, sklearn pipelines
- Your approach or initial thoughts on parallelization strategy
- Estimated time to complete

**Ideal contributor profile:** ML engineer with production experience using Optuna or Hyperopt; deep familiarity with XGBoost and LightGBM hyperparameter semantics (especially `scale_pos_weight`, `is_unbalance`) is essential.
