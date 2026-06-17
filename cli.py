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
def score(
    no_submit: bool = typer.Option(False, "--no-submit", help="Run scoring without on-chain submission"),
    use_async: bool = typer.Option(False, "--async", help="Use async pipeline for concurrent I/O and batched inference"),
) -> None:
    """Run the detection pipeline against live Horizon data and store the resulting scores."""
    import asyncio

    import run_pipeline

    if use_async:
        scores = asyncio.run(run_pipeline.async_run())
    else:
        scores = run_pipeline.run(no_submit=no_submit)
    for s in scores:
        logger.info("%s %s -> score=%d (benford=%s, ml=%s, confidence=%d)", s.wallet, s.asset_pair, s.score, s.benford_flag, s.ml_flag, s.confidence)


@app.command("eval-robustness")
def eval_robustness(
    n_trials: int = typer.Option(5, help="Adversarial dataset repetitions per strategy (more = slower but stabler)"),
    seed: int = typer.Option(42, help="Random seed"),
    n_normal_accounts: int = typer.Option(60, help="Normal accounts for training"),
    n_wash_rings: int = typer.Option(10, help="Wash rings for training"),
    ring_size: int = typer.Option(3, help="Accounts per ring for training"),
    adversarial_augment: bool = typer.Option(True, help="Use adversarial augmentation during training"),
) -> None:
    """Train the ensemble then evaluate robustness under each evasion strategy.

    Prints a table of AUC-ROC, F1, and Delta-AUC per strategy, plus a row
    showing performance after adversarial training.

    Target: Delta-AUC for \"all strategies\" must be > -0.10 with adversarial
    augmentation (i.e. recovery of ≥ 70 % of the performance gap vs. baseline).
    """
    from detection.dataset import build_training_dataset
    from detection.model_training import train_ensemble
    from detection.robustness_eval import evaluate_robustness
    from ingestion.synthetic_data import generate_synthetic_dataset

    # Train a baseline model (no augmentation) for comparison
    logger.info("Training baseline model (no adversarial augmentation)…")
    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts, n_wash_rings=n_wash_rings, ring_size=ring_size, seed=seed
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    baseline_results = train_ensemble(df, adversarial_augment=False)
    baseline_models = {k: v["model"] for k, v in baseline_results.items()}

    logger.info("Evaluating robustness of baseline model…")
    robustness = evaluate_robustness(baseline_models, n_trials=n_trials, seed=seed)

    # Train an adversarially-augmented model
    logger.info("Training adversarially-augmented model…")
    adv_results = train_ensemble(df, adversarial_augment=adversarial_augment)
    adv_models = {k: v["model"] for k, v in adv_results.items()}

    logger.info("Evaluating robustness of augmented model…")
    adv_robustness = evaluate_robustness(adv_models, n_trials=n_trials, seed=seed)

    # --- Print table ---
    header = f"{'Strategy':<24} {'AUC-ROC':>8} {'F1':>6} {'Delta-AUC':>10}"
    divider = "─" * len(header)
    typer.echo(divider)
    typer.echo(header)
    typer.echo(divider)

    def _row(label: str, entry: dict, suffix: str = "") -> str:
        auc = entry.get("auc_roc", float("nan"))
        f1 = entry.get("f1", float("nan"))
        delta = entry.get("delta_auc")
        delta_str = f"{delta:+.3f}" if delta is not None else "—"
        return f"{label + suffix:<24} {auc:>8.3f} {f1:>6.3f} {delta_str:>10}"

    typer.echo(_row("Baseline", robustness["baseline"]))

    from ingestion.adversarial_data import ALL_STRATEGIES
    for strategy in ALL_STRATEGIES:
        if strategy in robustness:
            label = strategy.replace("_", " ").title()
            typer.echo(_row(label, robustness[strategy]))

    typer.echo(_row("All strategies", robustness["all_strategies"]))
    typer.echo(_row("Adv. training", adv_robustness["all_strategies"], " ←"))
    typer.echo(divider)

    # Check target: delta-AUC for all_strategies with adv training must be > -0.10
    adv_delta = adv_robustness["all_strategies"].get("delta_auc", float("nan"))
    if adv_delta > -0.10:
        typer.echo(f"✅ Target met: adversarial training delta-AUC = {adv_delta:+.3f} (> -0.10)")
    else:
        typer.echo(f"⚠️  Target missed: adversarial training delta-AUC = {adv_delta:+.3f} (target > -0.10)")


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
