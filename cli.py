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
    import os

    from config.settings import settings
    from detection.dataset import build_training_dataset
    from detection.model_training import save_models, train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, account_metadata, events, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts, n_wash_rings=n_wash_rings, ring_size=ring_size, seed=seed
    )
    df = build_training_dataset(trades, labels, account_metadata=account_metadata, order_book_events=events)

    # Save training dataset for drift detection reference
    os.makedirs(settings.model_dir, exist_ok=True)
    training_dataset_path = os.path.join(settings.model_dir, "training_reference.csv")
    df.to_csv(training_dataset_path, index=False)
    logger.info("Saved training reference to %s", training_dataset_path)

    results = train_ensemble(df)
    for name, result in results.items():
        logger.info("%s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

    save_models(results, training_dataset_path=training_dataset_path)
    logger.info("Saved models to %s", settings.model_dir)


@app.command("retrain-check")
def retrain_check(
    psi_threshold: float = typer.Option(0.20, help="PSI threshold for drift detection"),
    min_drifted_features: int = typer.Option(3, help="Minimum number of drifted features to trigger retraining"),
    force_retrain: bool = typer.Option(False, help="Force retraining even if no drift detected"),
) -> None:
    """Check for distribution drift and retrain the ensemble if detected.

    Computes Population Stability Index (PSI) on recent scored features
    against the training reference distribution. If drift is detected
    (>= min_drifted_features with PSI > psi_threshold), triggers a
    full retraining cycle. New model is promoted only if it matches or
    outperforms the previous model on AUC-ROC.
    """
    import json
    import os
    from datetime import datetime

    from config.settings import settings
    from detection.dataset import build_training_dataset
    from detection.drift_monitor import is_drift_detected, run_drift_report
    from detection.model_registry import (
        get_current_version,
        rollback_model,
    )
    from detection.model_training import save_models, train_ensemble
    from detection.storage import save_drift_report, save_retrain_run
    from ingestion.synthetic_data import generate_synthetic_dataset

    # Read training metadata
    metadata_path = os.path.join(settings.model_dir, "training_metadata.json")
    if not os.path.exists(metadata_path):
        logger.warning("Training metadata not found at %s; cannot run drift check", metadata_path)
        return

    with open(metadata_path, "r") as f:
        metadata = json.load(f)

    training_dataset_path = metadata.get("training_dataset_path", "")

    # Run drift report
    report = run_drift_report(training_dataset_path)
    if not report:
        logger.warning("Could not compute drift report; skipping retrain check")
        return

    logger.info("Drift report: %s", report)

    # Check if drift detected
    drift_detected = is_drift_detected(report, psi_threshold=psi_threshold, min_drifted_features=min_drifted_features)

    drift_report_id = save_drift_report(
        drift_detected=drift_detected,
        psi_report=report,
        psi_threshold=psi_threshold,
        min_drifted_features=min_drifted_features,
    )

    if not drift_detected and not force_retrain:
        logger.info("No drift detected; skipping retrain")
        return

    if force_retrain:
        logger.info("Forcing retrain (force_retrain=True)")

    # Retrain the ensemble
    logger.info("Starting retrain cycle…")
    trades, account_metadata, events, labels = generate_synthetic_dataset(
        n_normal_accounts=60, n_wash_rings=10, ring_size=3, seed=42
    )
    df = build_training_dataset(trades, labels, account_metadata=account_metadata, order_book_events=events)

    new_results = train_ensemble(df)
    for name, result in new_results.items():
        logger.info("New %s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

    # Compare new models with previous models
    previous_metrics = metadata.get("model_metrics", {})
    promoted = False
    old_versions = {model_name: get_current_version(model_name, settings.model_dir) for model_name in new_results}
    auc_by_model: dict[str, tuple[float, float]] = {}

    for model_name, new_result in new_results.items():
        old_auc = previous_metrics.get(model_name, {}).get("auc_roc", 0.0)
        new_auc = new_result.get("auc_roc", 0.0)
        auc_by_model[model_name] = (old_auc, new_auc)

        if new_auc >= old_auc:
            logger.info(
                "%s: AUC-ROC improved from %.3f to %.3f; promoting",
                model_name,
                old_auc,
                new_auc,
            )
            promoted = True
        else:
            logger.warning(
                "%s: AUC-ROC degraded from %.3f to %.3f; reverting to previous version",
                model_name,
                old_auc,
                new_auc,
            )

    # Save models and metadata
    training_dataset_path = os.path.join(settings.model_dir, "training_reference.csv")
    df.to_csv(training_dataset_path, index=False)

    if promoted:
        save_models(new_results, training_dataset_path=training_dataset_path)
        logger.info("Promoted new models to production")
    else:
        logger.info("New models not promoted; keeping previous versions")
        for model_name, old_version in old_versions.items():
            if old_version:
                rollback_model(model_name, old_version, settings.model_dir)

    for model_name in new_results:
        old_auc, new_auc = auc_by_model[model_name]
        save_retrain_run(
            drift_report_id=drift_report_id,
            model_name=model_name,
            old_version=old_versions[model_name],
            new_version=get_current_version(model_name, settings.model_dir),
            old_auc_roc=old_auc,
            new_auc_roc=new_auc,
            promoted=promoted,
            forced=force_retrain,
        )

    # Write drift report
    drift_report_dir = "./drift_reports"
    os.makedirs(drift_report_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = os.path.join(drift_report_dir, f"{timestamp}.json")
    with open(report_path, "w") as f:
        json.dump(
            {
                "timestamp": timestamp,
                "drift_detected": drift_detected,
                "psi_report": report,
                "promoted": promoted,
                "new_model_metrics": {k: v.get("auc_roc") for k, v in new_results.items()},
            },
            f,
            indent=2,
        )
    logger.info("Wrote drift report to %s", report_path)


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


@app.command("db-migrate")
def db_migrate(
    db_path: str = typer.Option(None, "--db-path", help="Path to the SQLite database (defaults to LEDGERLENS_DB_PATH)"),
) -> None:
    """Apply any pending schema migrations to the database and report the result."""
    from detection.storage import _connect, get_schema_version, migrate_db

    with _connect(db_path) as conn:
        before = get_schema_version(conn)

    with _connect(db_path) as conn:
        applied = migrate_db(conn)
        after = get_schema_version(conn)

    if applied:
        typer.echo(f"Migrated from version {before} → {after}. Applied: {applied}")
    else:
        typer.echo(f"Database already at latest schema version {after}. No migrations applied.")


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
