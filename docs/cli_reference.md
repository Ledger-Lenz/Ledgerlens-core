# CLI Reference

## Shell Auto-Completion

LedgerLens CLI supports tab-completion for Bash, Zsh, and Fish shells.

### Installation

**Bash** — add to `~/.bashrc`:
```bash
eval "$(python -m cli completion --shell bash)"
```

**Zsh** — add to `~/.zshrc`:
```bash
eval "$(python -m cli completion --shell zsh)"
```

**Fish** — add to `~/.config/fish/config.fish`:
```fish
python -m cli completion --shell fish | source
```

After adding the line, restart your shell or run `source ~/.bashrc` (or equivalent).

### What Gets Completed

- Subcommand names: `generate-data`, `train`, `score`, `serve`, `stream`,
  `retrain-check`, `db-migrate`, `reweight`, `sign-models`, `webhook-worker`,
  `eval-robustness`, `robustness-eval`, `completion`, `federated`
- Common flags: `--output`, `--shell`, `--host`, `--port`
- Enum values for `--shell`: `bash`, `zsh`, `fish`

## Commands

| Command | Description |
|---------|-------------|
| `generate-data` | Generate synthetic trade dataset with labelled wash-trading rings |
| `train` | Train the RF/XGBoost/LightGBM ensemble on synthetic data |
| `score` | Run the detection pipeline and store scores |
| `serve` | Serve the local FastAPI API |
| `stream` | Stream trades from Horizon SSE in real-time |
| `retrain-check` | Check for distribution drift and retrain if needed |
| `db-migrate` | Apply pending schema migrations |
| `reweight` | Update ensemble weights from feedback |
| `sign-models` | Sign model artifacts with HMAC-SHA256 |
| `webhook-worker` | Run the webhook delivery worker |
| `eval-robustness` | Evaluate adversarial robustness |
| `robustness-eval` | Run PGD attacks on test split |
| `completion` | Print shell completion script |
| `federated server` | Start the federated aggregation server |
| `federated join` | Join the federated training pool |
