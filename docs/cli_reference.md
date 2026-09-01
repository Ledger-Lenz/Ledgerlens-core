# CLI Reference

LedgerLens provides a `ledgerlens` CLI built with [Typer](https://typer.tiangolo.com/).

## Commands

| Command | Description |
|---------|-------------|
| `generate-data` | Generate a synthetic trade dataset with labelled wash-trading rings |
| `generate-adversarial` | Generate a labelled adversarial feature dataset with a specific evasion strategy |
| `train` | Train the RF/XGBoost/LightGBM ensemble on a synthetic dataset and save it to `MODEL_DIR` |
| `generate-model-card` | Generate a model card for a specific model version on demand |
| `archive-features` | Archive feature distribution snapshots older than cutoff_days to Parquet cold tier |
| `retrain-check` | Check for distribution drift and retrain the ensemble if detected |
| `historical-load` | Backfill historical Horizon trades with bounded parallel workers |
| `export-parquet` | Export Trade records from SQLite to date-partitioned Parquet files |
| `eval-robustness` | Train the ensemble then evaluate robustness under each evasion strategy |
| `robustness-eval` | Run PGD attacks on the test split and produce a RobustnessReport saved to DB |
| `serve` | Serve the local read-only API (`api.main:app`) |
| `stream` | Stream trades from Horizon SSE and score incrementally per wallet |
| `db-migrate` | Apply any pending schema migrations to the database and report the result |
| `dlq-replay` | Replay pending Soroban dead-letter submissions |
| `governance-close-expired` | Close all active governance proposals whose voting period has expired |
| `reweight` | Update ensemble weights from recent feedback using Bayesian Model Averaging |
| `sign-models` | Backfill HMAC-SHA256 signatures for every `.joblib` in `model_dir` |
| `generate-signing-key` | Generate a new ED25519 keypair for model signing |
| `verify-models` | Verify all model artifacts in `MODEL_DIR` using ED25519 signatures; exits non-zero if any fail |
| `compute-embeddings` | Compute and store GNN embeddings for all wallets in the last N days of trades |
| `webhook-worker` | Run the webhook delivery worker as a foreground process |
| `analyst-lock-sweep` | Run the analyst case lock expiry sweep as a foreground process |
| `fuzz-check` | Run each Atheris fuzz harness for a bounded duration and exit non-zero on any crash |
| `red-team` | Run automated red-team attack campaigns and exit 1 if any campaign fails (CI gate) |
| `publish-backlog` | Replay existing SQLite `risk_scores` rows onto the event bus |
| `dedup-audit` | Report duplicate-detection statistics and details since a given ISO-8601 datetime |
| `grpc-serve` | Run the gRPC Internal Scoring Service sidecar |
| `rotate-sweep` | Revoke keys whose rotation grace period has elapsed |
| `re-encrypt-webhook-secrets` | Decrypt webhook secrets using either current or previous keys, and re-encrypt under the current key |
| `score` | Run the detection pipeline against live Horizon data (`--no-submit`, `--async`, `--bootstrap-threshold`, `--bootstrap-samples`) |
| `score bulk` | Score a CSV list of Stellar wallets against the local detection pipeline |
| `backtest run` | Run the backtesting pipeline against a labelled historical dataset |
| `federated server` | Start the federated aggregation server as a standalone process |
| `federated admit` | Authorize a `participant_id` to register with the federated server |
| `federated join` | Join the federated training pool as an exchange operator |
| `config validate` | Load and validate configuration, printing all settings (secrets masked) |
| `db retention` | Archive records older than their TTL to Parquet and purge from SQLite |
| `db migrate` | Apply pending Alembic migrations (equivalent to `alembic upgrade head`) |
| `db rollback` | Roll back the most recent Alembic migration (equivalent to `alembic downgrade -1`) |
| `api export-schema` | Export the auto-generated OpenAPI 3.1 schema to a JSON file |
| `benford calibrate` | Recompute the Benford digit-frequency baseline for an asset pair from stored trades |

Run `python cli.py --help` or `python cli.py <command> --help` for full option
lists — this table is generated from that output and may drift; if it looks
stale, regenerate it the same way.

## Shell Completion

This CLI is built with [Typer](https://typer.tiangolo.com/), which provides
completion via built-in options rather than a dedicated subcommand:

```bash
# Install completion for your current shell (writes to your shell's rc file):
python cli.py --install-completion

# Print the completion script instead of installing it:
python cli.py --show-completion
```

### What's Completed

- Subcommand names (e.g. `score`, `stream`, `db migrate`)
- Documented flags for the current command

`stream --reset-cursor` deletes the durable Horizon paging-token checkpoint
before connecting — both the legacy JSON file at `CURSOR_CHECKPOINT_PATH`
(must be inside `DATA_DIR`) and the unified SQLite checkpoint that also
covers rolling-window state (see
[Ingestion](ingestion.md#horizon-cursor-checkpointing)).

The stream command also accepts:

- `--queue-depth N`: maximum buffered trade count (default
  `STREAMER_QUEUE_MAXSIZE=1000`).
- `--overflow-strategy block|drop_newest|drop_oldest`: behavior when the queue
  is full (default `STREAMER_OVERFLOW_STRATEGY=drop_oldest`).

See [Ingestion](ingestion.md#flow-control-and-backpressure) for policy
trade-offs and recovery guidance.

## Historical loading

```bash
python cli.py historical-load \
  --start 2026-05-01T00:00:00Z \
  --end 2026-05-31T00:00:00Z \
  --asset-pair XLM/USDC \
  --concurrency 8 \
  --chunk-hours 6 \
  --resume
```

`--start` is inclusive and `--end` is exclusive. Use `--no-resume` to
re-fetch every chunk; duplicate paging tokens remain harmless because trade
writes are idempotent.
