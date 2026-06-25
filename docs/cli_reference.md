# CLI Reference

LedgerLens provides a `ledgerlens` CLI built with [Typer](https://typer.tiangolo.com/).

## Commands

| Command | Description |
|---------|-------------|
| `generate-data` | Generate synthetic trades/labels to CSV |
| `train` | Train RF/XGBoost/LightGBM ensemble on synthetic data |
| `score` | Run detection pipeline against live Horizon data |
| `serve` | Serve local FastAPI app |
| `stream` | Stream trades from Horizon SSE and score in rolling batches |
| `report` | Generate a compliance audit report for a wallet |
| `completion` | Print shell completion script |
| `retrain-check` | Check distribution drift and retrain if needed |
| `eval-robustness` | Evaluate adversarial robustness |
| `robustness-eval` | Run PGD attacks on the test split |
| `db-migrate` | Apply pending SQLite schema migrations |
| `reweight` | Update ensemble weights from feedback |
| `sign-models` | Backfill HMAC-SHA256 signatures for `.joblib` files |
| `webhook-worker` | Run webhook delivery worker |
| `federated server` | Start federated aggregation server |
| `federated join` | Join federated training pool |

## Shell Completion

### Installation

Add the following to your shell's configuration file:

**Bash** (`~/.bashrc`):
```bash
eval "$(ledgerlens completion --shell bash)"
```

**Zsh** (`~/.zshrc`):
```zsh
eval "$(ledgerlens completion --shell zsh)"
```

**Fish** (`~/.config/fish/config.fish`):
```fish
ledgerlens completion --shell fish | source
```

### What's Completed

- Subcommand names (e.g. `score`, `stream`, `report`, `completion`)
- Common flags (e.g. `--output`, `--concurrency`, `--date`)
- `--shell` enum values (`bash`, `zsh`, `fish`)
