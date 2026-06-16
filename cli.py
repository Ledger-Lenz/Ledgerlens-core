"""LedgerLens command-line interface.

Convenience wrapper around the pieces of the detection engine that are
otherwise run as separate scripts/modules:

    python -m cli generate-data   # synthetic trades + labels -> CSV
    python -m cli train           # train the ensemble on synthetic data
    python -m cli score            # run the detection pipeline and store scores
    python -m cli serve            # serve the local FastAPI app
    python -m cli webhook-worker   # run the webhook delivery worker
"""

import logging

import typer

app = typer.Typer(help="LedgerLens detection engine CLI")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ledgerlens.cli")


@app.command("generate-data")
def generate_data(
    out_dir: str = typer.Option("./data/synthetic", help="Directory to write trades.csv and labels.csv to"),
    n_normal_accounts: int = typer.Option(60, help="Number of normal (non-wash) accounts"),
    n_wash_rings: int = typer.Option(10, help="Number of wash-trading rings"),
    ring_size: int = typer.Option(3, help="Accounts per wash ring"),
    seed: int = typer.Option(42, help="Random seed for reproducibility"),
) -> None:
    """Generate a synthetic trade dataset with labelled wash-trading rings."""
    import os

    import pandas as pd

    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, account_metadata, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts, n_wash_rings=n_wash_rings, ring_size=ring_size, seed=seed
    )

    os.makedirs(out_dir, exist_ok=True)
    trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    events.to_csv(os.path.join(out_dir, "order_book_events.csv"), index=False)
    pd.DataFrame(
        [{"wallet": w, "label": label, **account_metadata.get(w, {})} for w, label in labels.items()]
    ).to_csv(os.path.join(out_dir, "labels.csv"), index=False)

    logger.info("Wrote %d trades, %d events, %d labelled accounts to %s", len(trades), len(events), len(labels), out_dir)


@app.command("train")
def train(
    n_normal_accounts: int = typer.Option(60, help="Number of normal (non-wash) accounts"),
    n_wash_rings: int = typer.Option(10, help="Number of wash-trading rings"),
    ring_size: int = typer.Option(3, help="Accounts per wash ring"),
    seed: int = typer.Option(42, help="Random seed for reproducibility"),
) -> None:
    """Train the RF/XGBoost/LightGBM ensemble on a synthetic dataset and save it to `MODEL_DIR`."""
    from config.settings import settings
    from detection.dataset import build_training_dataset
    from detection.model_training import save_models, train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, account_metadata, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts, n_wash_rings=n_wash_rings, ring_size=ring_size, seed=seed
    )
    df = build_training_dataset(trades, labels, account_metadata=account_metadata, order_book_events=events)

    results = train_ensemble(df)
    for name, result in results.items():
        logger.info("%s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

    save_models(results)
    logger.info("Saved models to %s", settings.model_dir)


@app.command("score")
def score() -> None:
    """Run the detection pipeline against live Horizon data and store the resulting scores."""
    import run_pipeline

    scores = run_pipeline.run()
    for s in scores:
        logger.info("%s %s -> score=%d (benford=%s, ml=%s, confidence=%d)", s.wallet, s.asset_pair, s.score, s.benford_flag, s.ml_flag, s.confidence)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8000, help="Port to bind to"),
    reload: bool = typer.Option(False, help="Enable auto-reload for development"),
) -> None:
    """Serve the local read-only API (`api.main:app`)."""
    import uvicorn

    uvicorn.run("api.main:app", host=host, port=port, reload=reload)


@app.command("webhook-worker")
def webhook_worker(
    interval: float = typer.Option(5.0, "--interval", help="Poll interval in seconds"),
) -> None:
    """Run the webhook delivery worker as a foreground process."""
    import asyncio

    from detection.webhook_worker import run_delivery_worker

    asyncio.run(run_delivery_worker(interval_seconds=interval))


if __name__ == "__main__":
    app()
