---
title: "Build Historical Backtesting Framework for Model Performance Evaluation on Past Data"
labels: ["difficulty: advanced", "area: ml", "type: feature"]
assignees: []
---

## Summary
There is no framework for evaluating how the LedgerLens models would have performed on historical Stellar trade data with known outcomes (confirmed wash-trading cases from public enforcement actions). A backtesting framework loads a labelled historical dataset, runs the current model pipeline over it, and computes precision/recall/F1 relative to ground truth — enabling evidence-based model improvement and regression testing.

## Objectives
- [ ] Build `backtesting/backtest_runner.py` that loads a labelled CSV (wallet, label, start_date, end_date)
- [ ] Run the full feature extraction and scoring pipeline over the historical date range for each wallet
- [ ] Compute precision, recall, F1, AUC-ROC, and average precision at multiple score thresholds
- [ ] Output a backtest report: `backtest_results_YYYY-MM-DD.json` with per-wallet scores and aggregate metrics
- [ ] `cli.py backtest run --dataset data/backtest/known_cases.csv --threshold 70`
- [ ] Include a small synthetic labelled dataset in `data/backtest/` for CI validation

## Definition of Done
- [ ] Backtest runner processes the synthetic dataset without error
- [ ] AUC-ROC on synthetic dataset ≥ 0.85 (synthetic cases designed to be detectable)
- [ ] Report JSON schema documented in `docs/backtesting.md`
- [ ] CI runs backtest and fails if AUC-ROC drops below 0.80
