# LedgerLens Backtesting

This directory contains the backtesting framework used to evaluate LedgerLens
detection models against labelled historical data.

## Contents

- **`backtest_runner.py`** — Loads a labelled CSV dataset
  (`wallet,label,start_date,end_date`), runs the feature-extraction and scoring
  pipeline for each wallet, and computes precision / recall / F1 / AUC-ROC /
  average precision at configurable score thresholds (including a threshold
  sweep). Reports are written as JSON via `save_report()`.
- **`__init__.py`** — Package marker that makes `backtesting` importable.

## Known-cases dataset

The canonical dataset lives at [`data/backtest/known_cases.csv`](../data/backtest/known_cases.csv).
Each row represents one Stellar wallet:

| Column | Meaning |
|--------|---------|
| `wallet` | Stellar wallet address (G…-prefixed public key) |
| `label` | `1` = confirmed wash trader, `0` = clean |
| `start_date` / `end_date` | Observation window used when scoring the wallet |

## Walk-forward mode (recommended default)

**Use walk-forward evaluation for all model evaluation going forward.**
Scoring a whole labelled history with a model trained on overlapping data
lets information from the evaluation period leak into training (lookahead
bias), which inflates metrics relative to real deployment.

`run_walk_forward_backtest(df, config, fit, predict, threshold=70)` trains
strictly on data observed before a boundary `T`, evaluates strictly on
`[T, T + test_window)`, then moves `T` forward by `step`. Each fold is
checked by `assert_no_lookahead`, which raises `LookaheadBiasError` if any
training feature or label postdates the fold cutoff or any row appears in
both windows.

```python
import pandas as pd
from backtesting.backtest_runner import WalkForwardConfig, run_walk_forward_backtest

config = WalkForwardConfig(
    train_window=pd.Timedelta(days=90),   # None = expanding window over all prior history
    test_window=pd.Timedelta(days=30),
    step=pd.Timedelta(days=30),
    time_column="timestamp",              # when the features were observed
    label_time_column="label_known_at",   # optional: when the label became known
    gap=pd.Timedelta(days=1),             # optional embargo between train and test
)
report = run_walk_forward_backtest(df, config, fit=train_fn, predict=score_fn)
report.aggregate["auc_roc"], [f.metrics for f in report.folds]
```

| Setting | Meaning |
|---------|---------|
| `train_window` | Rolling training window length; `None` for an expanding window |
| `test_window` | Length of each evaluation window |
| `step` | How far the train/test boundary advances per fold |
| `time_column` | Feature-observation timestamp column |
| `label_time_column` | Optional; rows whose label was not yet known at the cutoff are excluded from training |
| `gap` | Optional embargo so trailing-window features computed near the boundary cannot straddle it |

`fit(train_df)` returns a model; `predict(model, test_df)` returns 0–100
scores. `tests/test_backtest_runner.py::TestWalkForward` includes a synthetic
lookahead-bias trap: a memorising model that scores perfectly under a leaky
evaluation and exactly at chance under walk-forward.

## Running a backtest

From the repository root (after training models with `python cli.py train`):

```bash
python cli.py backtest run
```

The default dataset is `data/backtest/known_cases.csv`. Useful options include
`--threshold`, `--output-dir`, and `--model-dir`. The JSON report is written to
the current directory as `backtest_results_YYYY-MM-DD.json`.

## Further reading

For deeper documentation on the detection pipeline these backtests exercise, see:

- [docs/benford_analysis.md](../docs/benford_analysis.md) — Benford's Law analysis
- [docs/ensemble_stacking.md](../docs/ensemble_stacking.md) — Ensemble ML scoring
- [docs/index.md](../docs/index.md) — LedgerLens documentation index
