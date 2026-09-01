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
