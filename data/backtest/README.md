# `data/backtest/`

## `known_cases.csv`

Labelled dataset consumed by [`backtesting/backtest_runner.py`](../../backtesting/backtest_runner.py)
(via `load_labelled_dataset`) to evaluate model precision/recall/F1/AUC-ROC
against known wallets. It's the default `--dataset` for `cli.py backtest run`.

### Schema

| Column       | Description                                                                 |
|--------------|-------------------------------------------------------------------------------|
| `wallet`     | Stellar wallet address (public key, `G...`) being evaluated.                 |
| `label`      | Ground-truth classification: `1` = confirmed wash trader, `0` = clean.       |
| `start_date` | Start (`YYYY-MM-DD`) of the observation window used for feature extraction.  |
| `end_date`   | End (`YYYY-MM-DD`) of the observation window used for feature extraction.    |

`wallet` and `label` are required by `load_labelled_dataset`; `start_date`/
`end_date` are passed through to feature extraction to bound the trade
history considered for that wallet.

### Provenance

The current `known_cases.csv` is a **synthetic dataset**, introduced alongside
the backtesting framework itself (`99b799a`, "add backtesting framework for
model evaluation on labelled data") as a placeholder/CI dataset — the wallet
addresses are generated patterns, not real Stellar accounts. Whether any
real, confirmed incident data has since been mixed in is **TBD — needs
investigation**; check with the team before treating existing rows as
ground truth for anything beyond CI/smoke testing.

### Adding a new known case

1. Append a row with a real or synthetic `wallet`, the correct `label`
   (`1`/`0`), and the `start_date`/`end_date` window over which the label
   applies.
2. No changes to `backtesting/backtest_runner.py` are required — it reads
   whatever rows are present in the CSV at the configured path.
3. Re-run `python cli.py backtest run` (or point `--dataset` at a separate
   file) to confirm the new row loads and scores as expected.
