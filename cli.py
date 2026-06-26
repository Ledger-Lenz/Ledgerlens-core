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
import os
import time
import tomllib
from pathlib import Path

import typer

try:
    _version_file = Path(__file__).resolve().parent / "pyproject.toml"
    with open(_version_file, "rb") as _vf:
        __version__ = tomllib.load(_vf)["project"]["version"]
except Exception:
    __version__ = "0.0.0"

app = typer.Typer(help="LedgerLens detection engine CLI")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ledgerlens.cli")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ledgerlens-core v{__version__}")
        raise typer.Exit()


@app.callback()
def _main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """LedgerLens detection engine CLI."""
    pass


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
    calibrate: bool = typer.Option(True, "--calibrate/--no-calibrate", help="Run conformal calibration after training"),
    experiment_name: str = typer.Option(None, "--experiment-name", help="MLflow experiment name for tracking"),
) -> None:
    """Train the RF/XGBoost/LightGBM ensemble on a synthetic dataset and save it to `MODEL_DIR`.

    Use --optimize to run 100-trial Bayesian hyperparameter optimization (Optuna TPE)
    before final training. Override trial budget with --n-trials and wall-clock cap
    with --timeout.
    """
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

    results = train_ensemble(df, calibrate=calibrate, experiment_name=experiment_name)
    for name, result in results.items():
        if name == "_calib":
            continue
        logger.info("%s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

    save_models(results, training_dataset_path=training_dataset_path)
    if calibrate and "_calib" in results:
        coverage = results["_calib"].get("coverage_avg", 0.0)
        logger.info("Conformal calibration complete (avg coverage=%.4f)", coverage)
    logger.info("Saved models to %s", settings.model_dir)


@app.command("retrain-check")
def retrain_check(
    psi_threshold: float = typer.Option(0.20, help="PSI threshold for drift detection"),
    min_drifted_features: int = typer.Option(3, help="Minimum number of drifted features to trigger retraining"),
    force_retrain: bool = typer.Option(False, help="Force retraining even if no drift detected"),
    force_promote: bool = typer.Option(False, "--force-promote", help="Override SHAP stability check and promote models anyway"),
) -> None:
    """Check for distribution drift and retrain the ensemble if detected.

    Checks both PSI-based feature distribution drift and analyst-labelled
    performance degradation. If F1 on recent feedback labels drops more than
    5 percentage points from the training baseline, retraining is triggered
    alongside drift-based retraining.

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
    from detection.drift_monitor import (
        check_psi_and_alert,
        compute_per_feature_psi,
        is_drift_detected,
        record_psi_snapshot,
        run_drift_report,
    )
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

    # Per-feature PSI tracking
    try:
        psi_dict = compute_per_feature_psi(training_dataset_path)
        record_psi_snapshot(psi_dict)
        check_psi_and_alert(psi_dict, psi_threshold=psi_threshold, min_drifted_features=min_drifted_features)
        logger.info("Per-feature PSI: %d features computed", len(psi_dict))
    except FileNotFoundError as exc:
        logger.warning("Per-feature PSI skipped: %s", exc)
    except Exception as exc:
        logger.warning("Per-feature PSI computation failed: %s", exc)

    # Check if drift detected
    drift_detected = is_drift_detected(report, psi_threshold=psi_threshold, min_drifted_features=min_drifted_features)

    drift_report_id = save_drift_report(
        drift_detected=drift_detected,
        psi_report=report,
        psi_threshold=psi_threshold,
        min_drifted_features=min_drifted_features,
    )

    # --- Performance degradation check (Issue-110) ---
    performance_triggered = False
    try:
        from detection.drift_monitor import ModelDegradationAlert, PerformanceMonitor

        monitor = PerformanceMonitor(db_path=settings.db_path)
        baseline_f1: float = metadata.get("model_metrics", {}).get("random_forest", {}).get("f1", 0.0)
        # Prefer val_f1_score when available (more representative than train split F1)
        baseline_f1 = metadata.get("val_f1_score", baseline_f1)
        if baseline_f1 == 0.0:
            logger.warning("baseline F1 not available in training_metadata.json; degradation check skipped")
        else:
            try:
                monitor.check_degradation(
                    baseline_f1=baseline_f1,
                    f1_threshold_drop=settings.performance_degradation_threshold,
                )
            except ModelDegradationAlert as alert:
                logger.warning("Model degradation detected: %s — triggering retrain", alert)
                performance_triggered = True
    except Exception as perf_exc:
        logger.warning("Performance degradation check failed: %s", perf_exc)

    if not drift_detected and not force_retrain and not performance_triggered:
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
    model_names = [k for k in new_results if k != "_calib"]
    for name in model_names:
        result = new_results[name]
        logger.info("New %s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

    # Compute SHAP importance summaries for new models
    from detection.feature_engineering import FEATURE_NAMES as _feat_names
    from detection.model_registry import (
        compare_importance_stability,
        compute_shap_summary,
        save_shap_importances,
    )

    new_shap: dict[str, list[dict]] = {}
    feature_cols = [c for c in df.columns if c in _feat_names]
    X_train = df[feature_cols].fillna(0.0).values
    for name in model_names:
        model_obj = new_results[name].get("model")
        if model_obj is not None:
            try:
                new_shap[name] = compute_shap_summary(model_obj, X_train, feature_cols)
            except Exception as shap_exc:
                logger.warning("SHAP summary for %s failed: %s", name, shap_exc)

    new_metadata_for_stability = {"version": "new", "shap_importances": new_shap}
    old_metadata_for_stability = {"version": metadata.get("version", "old"), "shap_importances": metadata.get("shap_importances", {})}

    stability = compare_importance_stability(old_metadata_for_stability, new_metadata_for_stability)
    if not stability.stable:
        logger.warning(
            "Feature importance stability check FAILED: min Spearman rho = %.3f "
            "(threshold: %.3f). Models NOT auto-promoted. "
            "Rerun with --force-promote to override.",
            min(stability.spearman_rho.values()) if stability.spearman_rho else 0.0,
            0.70,
        )
        if not force_promote:
            logger.info("Skipping promotion due to stability check failure")

    # Compare new models with previous models
    previous_metrics = metadata.get("model_metrics", {})
    promoted = False
    old_versions = {model_name: get_current_version(model_name, settings.model_dir) for model_name in model_names}
    auc_by_model: dict[str, tuple[float, float]] = {}

    for model_name in model_names:
        new_result = new_results[model_name]
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

    # Block promotion if stability check failed and --force-promote not set
    if not stability.stable and not force_promote:
        promoted = False

    # Save models and metadata
    training_dataset_path = os.path.join(settings.model_dir, "training_reference.csv")
    df.to_csv(training_dataset_path, index=False)

    if promoted:
        save_models(new_results, training_dataset_path=training_dataset_path)
        if new_shap:
            save_shap_importances(new_shap, settings.model_dir)
        logger.info("Promoted new models to production")
    else:
        logger.info("New models not promoted; keeping previous versions")
        for model_name, old_version in old_versions.items():
            if old_version:
                rollback_model(model_name, old_version, settings.model_dir)

    for model_name in model_names:
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
    drifted_features = [f for f, v in report.items() if v > psi_threshold]
    with open(report_path, "w") as f:
        json.dump(
            {
                "timestamp": timestamp,
                "drift_detected": drift_detected,
                "n_drifted_features": len(drifted_features),
                "psi_report": report,
                "per_feature_psi": report,
                "drifted_features": drifted_features,
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


@app.command("historical-load")
def historical_load(
    start: str = typer.Option(..., "--start", help="Inclusive ISO-8601 start time"),
    end: str = typer.Option(..., "--end", help="Exclusive ISO-8601 end time"),
    concurrency: int | None = typer.Option(
        None, "--concurrency", min=1, help="Maximum concurrent Horizon chunks"
    ),
    chunk_hours: float | None = typer.Option(
        None, "--chunk-hours", min=0.01, help="Hours per independent chunk"
    ),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Skip chunks already marked complete"
    ),
    asset_pair: str | None = typer.Option(
        None, "--asset-pair", help="Optional BASE/COUNTER asset pair"
    ),
) -> None:
    """Backfill historical Horizon trades with bounded parallel workers."""
    import asyncio
    from datetime import datetime

    from config.settings import settings as cfg
    from detection.storage import RiskScoreStore
    from ingestion.historical_loader import ParallelHistoricalLoader
    from ingestion.http_client import RetryingHorizonClient

    def parse_datetime(value: str, option: str) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise typer.BadParameter("must be an ISO-8601 datetime", param_hint=option) from exc

    start_time = parse_datetime(start, "--start")
    end_time = parse_datetime(end, "--end")

    async def run() -> None:
        worker_count = concurrency or cfg.historical_loader_concurrency
        hours = chunk_hours or cfg.historical_chunk_hours
        async with RetryingHorizonClient(
            cfg.horizon_url,
            max_concurrency=worker_count,
        ) as client:
            loader = ParallelHistoricalLoader(
                client=client,
                storage=RiskScoreStore(cfg.db_path),
                concurrency=worker_count,
                chunk_hours=hours,
                progress_path=Path(cfg.historical_progress_path),
            )
            result = await loader.load(
                start_time,
                end_time,
                asset_pair=asset_pair,
                resume=resume,
            )
            typer.echo(
                f"completed={result.completed_chunks} failed={result.failed_chunks} "
                f"skipped={result.skipped_chunks} records={result.total_records} "
                f"records_per_second={result.records_per_second:.1f}"
            )

    asyncio.run(run())


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
    baseline_results = train_ensemble(df, adversarial_augment=False, calibrate=False)
    baseline_models = {k: v["model"] for k, v in baseline_results.items()}

    logger.info("Evaluating robustness of baseline model…")
    robustness = evaluate_robustness(baseline_models, n_trials=n_trials, seed=seed)

    # Train an adversarially-augmented model
    logger.info("Training adversarially-augmented model…")
    adv_results = train_ensemble(df, adversarial_augment=adversarial_augment, calibrate=False)
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



@app.command("robustness-eval")
def robustness_eval(
    epsilon: float = typer.Option(0.1, help="Attack L2 budget"),
    steps: int = typer.Option(10, help="PGD steps (max 100)"),
    n_samples: int = typer.Option(200, help="Number of samples from test split to evaluate"),
) -> None:
    """Run PGD attacks on the test split and produce a RobustnessReport saved to DB."""
    if steps > 100:
        raise typer.BadParameter("--steps cannot exceed 100 for safety")

    from ingestion.synthetic_data import generate_synthetic_dataset
    from detection.dataset import build_training_dataset
    from detection.model_inference import load_models
    from detection.robustness_eval import compute_robustness_report
    from config.settings import settings

    trades, account_metadata, events, labels = generate_synthetic_dataset(n_normal_accounts=50, n_wash_rings=10, ring_size=4, seed=42)
    df = build_training_dataset(trades, labels, account_metadata=account_metadata, order_book_events=events)

    try:
        models = load_models(settings.model_dir)
    except FileNotFoundError:
        # train a temporary ensemble for evaluation
        from detection.model_training import train_ensemble

        logger.info("No trained models found; training temporary ensemble for robustness evaluation")
        results = train_ensemble(df, adversarial_augment=False)
        models = {k: v["model"] for k, v in results.items()}

    report = compute_robustness_report(models, df.sample(n=min(n_samples, len(df)), random_state=42), n_samples=200, epsilon=epsilon, steps=steps)
    typer.echo(report.model_dump_json(indent=2))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8000, help="Port to bind to"),
    reload: bool = typer.Option(False, help="Enable auto-reload for development"),
) -> None:
    """Serve the local read-only API (`api.main:app`)."""
    import uvicorn

    uvicorn.run("api.main:app", host=host, port=port, reload=reload)


@app.command("stream")
def stream(
    batch_size: int = typer.Option(500, "--batch-size", help="Number of trades to accumulate before scoring"),
    flush_interval: float = typer.Option(30.0, "--flush-interval", help="Maximum seconds to wait before flushing a partial batch"),
    checkpoint_interval: int = typer.Option(None, envvar="STREAM_CHECKPOINT_INTERVAL", help="Persist window state every N trades (default from settings)"),
    score_delta: int = typer.Option(None, envvar="STREAM_SCORE_DELTA_THRESHOLD", help="Minimum score change to emit an alert (default from settings)"),
    queue_depth: int = typer.Option(
        None,
        "--queue-depth",
        min=1,
        envvar="STREAMER_QUEUE_MAXSIZE",
        help="Maximum number of buffered Horizon trades (default from settings).",
    ),
    overflow_strategy: str = typer.Option(
        None,
        "--overflow-strategy",
        envvar="STREAMER_OVERFLOW_STRATEGY",
        help="Queue overflow policy: block, drop_newest, or drop_oldest.",
    ),
    reset_cursor: bool = typer.Option(
        False,
        "--reset-cursor",
        help="Delete the Horizon cursor checkpoint before streaming.",
    ),
) -> None:
    """Stream trades from Horizon SSE and score incrementally per wallet.

    Maintains per-wallet rolling windows (1h/4h/24h), recomputes features on
    each trade, and emits a RiskScore when the score changes by >= score_delta
    points. Window state is checkpointed to SQLite every checkpoint_interval
    trades. Graceful shutdown (SIGTERM/SIGINT) persists all in-memory state.
    """
    import signal
    import threading

    from config.settings import settings as cfg
    from detection.feature_engineering import FeatureEngineering
    from detection.model_inference import IncrementalScorer, ModelInference, load_models
    from detection.rolling_window import RollingWindowState, RollingWindowStore
    from detection.storage import init_db, save_scores
    from detection.webhook_queue import enqueue
    from detection.webhook_registry import get_matching_subscribers
    from ingestion.checkpoint import CursorCheckpoint, FlushPolicy, resolve_checkpoint_path
    from ingestion.horizon_streamer import stream_trades_with_cursor
    import api.main as api_main

    _chk_interval = checkpoint_interval if checkpoint_interval is not None else cfg.stream_checkpoint_interval
    _score_delta = score_delta if score_delta is not None else cfg.stream_score_delta_threshold
    _queue_depth = queue_depth if queue_depth is not None else cfg.streamer_queue_maxsize
    _overflow_strategy = (
        overflow_strategy
        if overflow_strategy is not None
        else cfg.streamer_overflow_strategy
    )
    if _overflow_strategy not in {"block", "drop_newest", "drop_oldest"}:
        raise typer.BadParameter(
            "must be block, drop_newest, or drop_oldest",
            param_hint="--overflow-strategy",
        )
    cursor_checkpoint = CursorCheckpoint(
        resolve_checkpoint_path(cfg.cursor_checkpoint_path, cfg.data_dir)
    )
    if reset_cursor:
        cursor_checkpoint.delete()
        logger.info("Reset Horizon cursor checkpoint")
    stored_cursor = cursor_checkpoint.load()
    cursor = stored_cursor or cfg.horizon_default_cursor
    if stored_cursor:
        logger.info("Resuming from cursor %s", cursor)
    else:
        logger.info("Starting fresh from cursor %s", cursor)
    cursor_flush_policy = FlushPolicy(
        max_events=cfg.cursor_flush_events,
        max_seconds=cfg.cursor_flush_seconds,
    )

    init_db()
    checkpoint_store = RollingWindowStore()
    window_state = RollingWindowState()
    checkpoint_store.load_all(window_state)

    try:
        models = load_models(cfg.model_dir)
    except FileNotFoundError:
        logger.error("No trained models found in %s — run `python cli.py train` first", cfg.model_dir)
        raise typer.Exit(1)

    fe = FeatureEngineering()
    scorer = IncrementalScorer(
        window_state=window_state,
        feature_engineering=fe,
        model_inference=ModelInference(models),
        score_delta_threshold=_score_delta,
    )

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received — checkpointing all window states…")
        checkpoint_store.save_all(scorer.window_state)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    trades_since_checkpoint = 0
    cursor_events_since_flush = 0
    last_cursor_flush = time.monotonic()
    last_cursor = cursor

    logger.info(
        "Starting incremental stream (checkpoint_interval=%d, score_delta=%d, "
        "queue_depth=%d, overflow_strategy=%s)",
        _chk_interval,
        _score_delta,
        _queue_depth,
        _overflow_strategy,
    )

    for trade, event_cursor in stream_trades_with_cursor(
        cursor=cursor, checkpoint=cursor_checkpoint
    ):
        if stop_event.is_set():
            break

        # Update stream status for /stream/status endpoint
        api_main._stream_status_update(trade)
        with api_main._stream_lock:
            api_main._stream_active_wallets = scorer.window_state.active_wallets

        result = scorer.score_on_trade(trade)
        if result:
            save_scores([result])
            try:
                subscribers = get_matching_subscribers(result)
                for sub in subscribers:
                    enqueue(sub.subscriber_id, result.model_dump(mode="json"))
            except Exception as exc:  # pragma: no cover
                logger.warning("Webhook dispatch error: %s", exc)

        last_cursor = event_cursor
        cursor_events_since_flush += 1
        now = time.monotonic()
        if cursor_flush_policy.should_flush(
            cursor_events_since_flush, last_cursor_flush, now
        ):
            cursor_checkpoint.save(last_cursor)
            cursor_events_since_flush = 0
            last_cursor_flush = now

        trades_since_checkpoint += 1
        if trades_since_checkpoint >= _chk_interval:
            checkpoint_store.save_all(scorer.window_state)
            trades_since_checkpoint = 0
            logger.debug("Checkpointed %d wallet windows", scorer.window_state.active_wallets)

    # Final checkpoint on clean exit
    checkpoint_store.save_all(scorer.window_state)
    if cursor_events_since_flush:
        cursor_checkpoint.save(last_cursor)
    logger.info("Stream stopped. Final checkpoint written.")


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


@app.command("dlq-replay")
def dlq_replay(
    limit: int = typer.Option(100, help="Max dead letters to replay per run"),
    dry_run: bool = typer.Option(False, help="Print DLQ contents without submitting"),
) -> None:
    """Replay pending Soroban dead-letter submissions.

    Processes oldest-first. Marks each as 'replayed' on success or 'failed'
    on persistent failure. Never removes rows from the DLQ.
    """
    import sqlite3
    from datetime import datetime, timezone

    from config.settings import settings
    from detection.soroban_publisher import (
        SorobanPublisher,
        SorobanSubmissionError,
        SorobanCircuitOpenError,
        get_dlq_entries,
        init_dlq_schema,
    )
    from detection.risk_score import RiskScore

    secret_key = os.environ.get("LEDGERLENS_SERVICE_SECRET_KEY", "")
    if not secret_key and not dry_run:
        typer.echo("ERROR: LEDGERLENS_SERVICE_SECRET_KEY is not set. Cannot replay.", err=True)
        raise typer.Exit(1)

    init_dlq_schema()
    items, total = get_dlq_entries(status="pending", page=1, page_size=limit)

    if not items:
        typer.echo("No pending DLQ entries.")
        return

    if dry_run:
        typer.echo(f"DRY RUN — {len(items)} pending item(s):")
        for item in items:
            typer.echo(f"  [{item['id']}] {item['wallet']}:{item['asset_pair']} score={item['score']} error={item['error_message']}")
        return

    publisher = SorobanPublisher(
        contract_id=os.environ.get("LEDGERLENS_SCORE_CONTRACT_ID", ""),
        secret_key=secret_key,
        soroban_rpc_url=os.environ.get("SOROBAN_RPC_URL", "https://soroban-testnet.stellar.org"),
        network_passphrase=os.environ.get("NETWORK_PASSPHRASE", "Test SDF Network ; September 2015"),
    )

    db_path = settings.db_path
    replayed = 0
    failed = 0

    for item in items:
        score_obj = RiskScore(
            wallet=item["wallet"],
            asset_pair=item["asset_pair"],
            score=item["score"],
            benford_flag=False,
            ml_flag=False,
            confidence=0,
            timestamp=datetime.fromtimestamp(item["ledger_timestamp"], tz=timezone.utc),
        )
        tx_hash = None
        status = "failed"
        try:
            tx_hash = publisher.submit_score(score_obj)
            status = "replayed"
            replayed += 1
            logger.info("DLQ item %d replayed: tx=%s", item["id"], tx_hash)
        except (SorobanSubmissionError, SorobanCircuitOpenError, Exception) as exc:
            logger.warning("DLQ item %d replay failed: %s", item["id"], exc)
            failed += 1

        now_iso = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE soroban_dead_letters SET status=?, replayed_at=?, replay_tx_hash=? WHERE id=?",
                (status, now_iso, tx_hash, item["id"]),
            )
            conn.commit()

    typer.echo(f"DLQ replay complete: {replayed} replayed, {failed} failed out of {len(items)} items.")


@app.command("governance-close-expired")
def governance_close_expired() -> None:
    """Close all active governance proposals whose voting period has expired.

    Tallies each expired proposal and sets its status to 'passed' or 'rejected'
    based on the quorum rule (>50% of committee votes 'for'). Designed to be
    called on a schedule (e.g., cron or systemd timer).
    """
    from detection.storage import init_db
    from detection.governance import GovernanceEngine

    init_db()
    engine = GovernanceEngine()
    closed = engine.close_expired()
    if not closed:
        typer.echo("No expired proposals to close.")
    else:
        for p in closed:
            typer.echo(f"Proposal {p.id} ({p.proposal_type}): {p.status}")


@app.command("reweight")
def reweight(
    days_back: int = typer.Option(7, "--days-back", help="Feedback window in days"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposed weights without writing"),
) -> None:
    """Update ensemble weights from recent feedback using Bayesian Model Averaging.

    Loads the last *days_back* days of scoring feedback, computes updated
    weights via :func:`detection.ensemble_reweighter.compute_updated_weights`,
    and (unless ``--dry-run``) writes them to ``models/ensemble_weights.json``.
    """
    from config.settings import settings
    from detection.ensemble_reweighter import apply_weights, compute_updated_weights
    from detection.feedback_store import get_recent_feedback

    feedback = get_recent_feedback(days_back=days_back)
    logger.info("Loaded %d feedback records from the last %d days", len(feedback), days_back)

    current = {
        "random_forest": settings.ensemble_weight_rf,
        "xgboost": settings.ensemble_weight_xgb,
        "lightgbm": settings.ensemble_weight_lgbm,
    }
    proposed = compute_updated_weights(feedback)

    header = f"{'Model':<20} {'Current':>10} {'Proposed':>10}"
    divider = "─" * len(header)
    typer.echo(divider)
    typer.echo(header)
    typer.echo(divider)
    for model in ("random_forest", "xgboost", "lightgbm"):
        typer.echo(f"{model:<20} {current[model]:>10.4f} {proposed[model]:>10.4f}")
    typer.echo(divider)

    if dry_run:
        typer.echo("Dry run — ensemble_weights.json not written.")
        return

    apply_weights(proposed, settings.model_dir)
    typer.echo("Wrote updated weights to ensemble_weights.json")


@app.command("sign-models")
def sign_models(
    model_dir: str = typer.Option(None, help="Defaults to settings.model_dir"),
) -> None:
    """Backfill HMAC-SHA256 signatures for every .joblib in model_dir.

    Idempotent: re-signs files whose content changed, skips already-valid ones.
    Run this once against trusted committed artifacts after setting
    LEDGERLENS_MODEL_SIGNING_KEY. Required before loading models with
    verification enabled.
    """
    import glob

    from config.settings import settings
    from detection.model_signing import ModelIntegrityError, sign_model_file, verify_model_file

    target_dir = model_dir or settings.model_dir
    signing_key = settings.model_signing_key.encode()

    if not signing_key:
        typer.echo("ERROR: LEDGERLENS_MODEL_SIGNING_KEY is not set.", err=True)
        raise typer.Exit(1)

    pattern = os.path.join(target_dir, "*.joblib")
    paths = glob.glob(pattern)
    if not paths:
        typer.echo(f"No .joblib files found in {target_dir}")
        return

    signed = []
    skipped = []
    for path in sorted(paths):
        try:
            verify_model_file(path, signing_key)
            skipped.append(path)
        except ModelIntegrityError:
            sign_model_file(path, signing_key)
            signed.append(path)

    for path in signed:
        logger.info("Signed: %s", path)
    for path in skipped:
        logger.info("Already valid, skipped: %s", path)

    typer.echo(f"Signed {len(signed)} file(s), skipped {len(skipped)} already-valid file(s).")


@app.command("generate-signing-key")
def generate_signing_key() -> None:
    """Generate a new ED25519 keypair for model signing.

    Prints public key (for settings.py) and private key (for environment).
    ONLY run this during initial setup or key rotation.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_b64 = base64.b64encode(
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode()
    pub_b64 = base64.b64encode(
        pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    typer.echo(
        f"Public key (embed in config/settings.py as MODEL_SIGNING_PUBLIC_KEY):\n{pub_b64}"
    )
    typer.echo(
        f"\nPrivate key (set as MODEL_SIGNING_PRIVATE_KEY env variable):\n{priv_b64}"
    )
    typer.echo("\nWARNING: Store the private key securely. It cannot be recovered.")


@app.command("verify-models")
def verify_models(
    model_dir: str = typer.Option(None, help="Defaults to settings.model_dir"),
) -> None:
    """Verify all model artifacts in MODEL_DIR using ED25519 signatures. Exits non-zero if any fail."""
    from config.settings import settings
    from detection.model_signing import ModelIntegrityError, get_model_signer

    target_dir = model_dir or settings.model_dir
    signer = get_model_signer()
    failures = []
    for model_file in sorted(Path(target_dir).glob("*.joblib")):
        try:
            signer.verify(model_file)
            typer.echo(f"OK: {model_file.name}")
        except ModelIntegrityError as e:
            typer.echo(f"FAIL: {e}", err=True)
            failures.append(model_file.name)
    if failures:
        raise typer.Exit(code=1)
    if not list(Path(target_dir).glob("*.joblib")):
        typer.echo(f"No .joblib files found in {target_dir}")


@app.command("webhook-worker")
def webhook_worker(
    interval: float = typer.Option(5.0, "--interval", help="Poll interval in seconds"),
) -> None:
    """Run the webhook delivery worker as a foreground process."""
    import asyncio

    from detection.webhook_worker import run_delivery_worker

    asyncio.run(run_delivery_worker(interval_seconds=interval))


backtest_app = typer.Typer(help="Backtesting framework for model evaluation")
app.add_typer(backtest_app, name="backtest")


@backtest_app.command("run")
def backtest_run(
    dataset: str = typer.Option("data/backtest/known_cases.csv", help="Path to labelled CSV dataset"),
    threshold: int = typer.Option(70, help="Score threshold for classification (0-100)"),
    output_dir: str = typer.Option(".", help="Directory to write the backtest report to"),
    model_dir: str = typer.Option(None, help="Model directory (defaults to settings.model_dir)"),
) -> None:
    """Run the backtesting pipeline against a labelled historical dataset.

    Loads the labelled CSV, runs feature extraction and model scoring,
    and outputs precision/recall/F1/AUC-ROC at the specified threshold.
    """
    from backtesting.backtest_runner import run_backtest, save_report

    report = run_backtest(
        dataset_path=dataset,
        threshold=threshold,
        model_dir=model_dir,
    )

    output_path = save_report(report, output_dir=output_dir)

    typer.echo(f"Backtest complete: {report.total_wallets} wallets evaluated")
    typer.echo(f"  Threshold: {threshold}")
    typer.echo(f"  Precision: {report.precision:.3f}")
    typer.echo(f"  Recall:    {report.recall:.3f}")
    typer.echo(f"  F1:        {report.f1:.3f}")
    typer.echo(f"  AUC-ROC:   {report.auc_roc:.3f}")
    typer.echo(f"  Avg Prec:  {report.average_precision:.3f}")
    typer.echo(f"Report saved to {output_path}")

    if report.thresholds_sweep:
        header = f"{'Threshold':>10} {'Precision':>10} {'Recall':>8} {'F1':>6}"
        typer.echo(header)
        for t in report.thresholds_sweep:
            typer.echo(f"{t['threshold']:>10} {t['precision']:>10.3f} {t['recall']:>8.3f} {t['f1']:>6.3f}")


federated_app = typer.Typer(help="Federated Learning commands for exchange operators")
app.add_typer(federated_app, name="federated")


@federated_app.command("server")
def federated_server(
    host: str = typer.Option(None, help="Host to bind (default from FEDERATED_SERVER_HOST)"),
    port: int = typer.Option(None, help="Port to bind (default from FEDERATED_SERVER_PORT)"),
    min_participants: int = typer.Option(None, help="Minimum quorum size before aggregation"),
) -> None:
    """Start the federated aggregation server as a standalone process."""
    import uvicorn

    from config.settings import settings as cfg
    from detection.federated.server import FederatedAggregationServer, federated_app as fl_app
    import detection.federated.server as fed_server_mod

    kwargs: dict = {}
    if min_participants is not None:
        kwargs["min_participants"] = min_participants
    fed_server_mod._server_instance = FederatedAggregationServer(**kwargs)

    bind_host = host or cfg.federated_server_host
    bind_port = port or cfg.federated_server_port
    logger.info("Starting federated server on %s:%d", bind_host, bind_port)
    uvicorn.run(fl_app, host=bind_host, port=bind_port)


@federated_app.command("join")
def federated_join(
    rounds: int = typer.Option(1, "--rounds", "-r", help="Number of federated rounds to participate in"),
    data_path: str = typer.Option(None, "--data-path", help="Path to operator's private labelled CSV"),
    server_url: str = typer.Option(None, "--server-url", help="Federated server URL"),
    operator_id: str = typer.Option("operator-0", "--operator-id", help="Unique operator identifier"),
) -> None:
    """Join the federated training pool as an exchange operator.

    If --data-path is omitted, a synthetic dataset is generated locally
    (useful for testing the protocol without real private data).
    """
    import httpx
    import base64

    import numpy as np

    from config.settings import settings as cfg
    from detection.dataset import build_training_dataset
    from detection.feature_engineering import FEATURE_NAMES
    from detection.federated.client import FederatedClient, _build_public_dataset
    from ingestion.synthetic_data import generate_synthetic_dataset
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    server_url = server_url or f"http://{cfg.federated_server_host}:{cfg.federated_server_port}"

    # Load or generate private training data
    if data_path:
        import pandas as pd
        df = pd.read_csv(data_path)
        X = df[FEATURE_NAMES].fillna(0.0).values.astype(np.float64)
        y = df["label"].values.astype(int)
    else:
        logger.info("No --data-path provided; using synthetic private dataset (seed=99)")
        trades, meta, events, labels = generate_synthetic_dataset(
            n_normal_accounts=30, n_wash_rings=5, ring_size=3, seed=99
        )
        df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
        X = df[FEATURE_NAMES].fillna(0.0).values.astype(np.float64)
        y = df["label"].values.astype(int)

    private_key = Ed25519PrivateKey.generate()
    client = FederatedClient(operator_id=operator_id, private_key=private_key)

    with httpx.Client(base_url=server_url, timeout=60.0) as http:
        # Register with server
        pub_der_b64 = base64.b64encode(client.public_key_der).decode()
        resp = http.post("/federated/register", json={
            "participant_id": operator_id,
            "public_key_der_b64": pub_der_b64,
        })
        resp.raise_for_status()
        logger.info("Registered with federated server as %s", operator_id)

        X_pub = _build_public_dataset()
        client.train_local_models(X, y)

        for round_num in range(rounds):
            # Fetch current global model
            resp = http.get("/federated/global-model")
            resp.raise_for_status()
            data = resp.json()
            round_id = data["round_id"]

            if data["global_soft_labels_b64"]:
                prev_global = np.frombuffer(
                    base64.b64decode(data["global_soft_labels_b64"]), dtype=np.float64
                )
            else:
                prev_global = np.full(len(X_pub), 0.5)

            soft_labels = client.compute_soft_labels(X_pub)
            delta = soft_labels - prev_global
            delta = client._clip_delta(delta)
            noisy_delta = client.inject_dp_noise(delta)
            noisy_soft_labels = np.clip(prev_global + noisy_delta, 0.0, 1.0)

            signature = client._sign_payload(noisy_soft_labels, len(y), round_id)

            resp = http.post("/federated/update", json={
                "participant_id": operator_id,
                "soft_labels_b64": base64.b64encode(noisy_soft_labels.tobytes()).decode(),
                "n_samples": len(y),
                "signature_b64": base64.b64encode(signature).decode(),
            })
            resp.raise_for_status()
            result = resp.json()
            logger.info("Round %d submitted: %s", round_num + 1, result)

            # Wait and fetch updated global model for distillation
            resp = http.get("/federated/global-model")
            resp.raise_for_status()
            data = resp.json()
            if data["global_soft_labels_b64"]:
                global_labels = np.frombuffer(
                    base64.b64decode(data["global_soft_labels_b64"]), dtype=np.float64
                )
                client.update_with_distilled_labels(X, y, X_pub, global_labels)
                logger.info("Round %d: distillation update applied", round_num + 1)

    logger.info("Federated participation complete (%d round(s))", rounds)


@app.command("red-team")
def red_team(
    model_dir: str = typer.Option("models", help="Directory containing trained model files"),
    n_samples: int = typer.Option(100, help="Number of seed samples per attack campaign"),
    evasion_threshold: float = typer.Option(0.05, help="Maximum allowed evasion rate (5%)"),
    report_dir: str = typer.Option("./red_team_reports", help="Directory to write campaign reports"),
    seed: int = typer.Option(42, help="Random seed for reproducibility"),
) -> None:
    """Run automated red-team attack campaigns and exit 1 if any campaign fails (CI gate)."""
    from detection.model_inference import load_models
    from detection.red_team.runner import RedTeamRunner

    logger.info("Loading models from %s", model_dir)
    models = load_models(model_dir=model_dir)

    # Build generic feature constraints from model feature list
    from detection.feature_engineering import FEATURE_NAMES
    feature_constraints = {f: {"min": 0.0, "max": 1.0, "mutable": True} for f in FEATURE_NAMES}

    runner = RedTeamRunner(
        model=models,
        feature_constraints=feature_constraints,
        evasion_threshold=evasion_threshold,
        report_dir=report_dir,
        seed=seed,
    )
    summary = runner.run_all_campaigns(n_samples=n_samples)
    path = runner.write_report(summary)
    typer.echo(f"Campaign report written to {path}")
    typer.echo(f"Overall result: {'PASSED' if summary.passed else 'FAILED'}")
    for c in summary.campaigns:
        typer.echo(f"  {c.attack_type.value}: evasion_rate={c.evasion_rate:.3f} {'OK' if c.passed else 'FAIL'}")

    if not summary.passed:
        raise typer.Exit(1)


config_app = typer.Typer(help="Configuration commands")
app.add_typer(config_app, name="config")

db_app = typer.Typer(help="Database maintenance commands")
app.add_typer(db_app, name="db")


@db_app.command("retention")
def db_retention(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be archived without making changes"),
    archive_root: str = typer.Option("./data/archive", "--archive-root", help="Root directory for Parquet archives"),
    db_path: str = typer.Option(None, "--db-path", help="Path to SQLite database (defaults to LEDGERLENS_DB_PATH)"),
) -> None:
    """Archive records older than their TTL to Parquet and purge from SQLite.

    Default TTLs: risk_scores=365d, feature_vectors=90d, alerts=730d.
    Use --dry-run to preview the archival plan without modifying the database.
    """
    from config.settings import settings as cfg
    from storage.retention import RetentionEngine

    engine = RetentionEngine(db_path=db_path or cfg.db_path, archive_root=archive_root)
    report = engine.run(dry_run=dry_run)

    prefix = "[DRY RUN] " if dry_run else ""
    for table, info in report.items():
        archived = info.get("rows_archived", 0)
        cutoff = info.get("cutoff_date", "")
        if info.get("skipped"):
            typer.echo(f"{prefix}{table}: table not found — skipped")
        elif archived == 0:
            typer.echo(f"{prefix}{table}: no rows older than {cutoff}")
        elif dry_run:
            typer.echo(f"{prefix}{table}: would archive {archived} rows older than {cutoff}")
        else:
            path = info.get("archive_path", "")
            typer.echo(f"{prefix}{table}: archived {archived} rows → {path}")


@config_app.command("validate")
def config_validate() -> None:
    """Load and validate configuration, printing all settings (secrets masked)."""
    import pydantic

    _SECRETS = {
        "ledgerlens_service_secret_key",
        "ledgerlens_admin_api_key",
        "ledgerlens_compliance_api_key",
        "ledgerlens_model_signing_key",
        "ledgerlens_webhook_encryption_key",
    }

    try:
        from config.settings import Settings
        s = Settings()
    except (pydantic.ValidationError, Exception) as exc:
        typer.echo(f"❌ Configuration invalid:\n{exc}", err=True)
        raise typer.Exit(1)

    typer.echo("✅ Configuration is valid\n")
    for name in Settings.model_fields:
        raw = getattr(s, name)
        value = "***" if name in _SECRETS and raw else raw
        typer.echo(f"  {name}={value}")


db_app = typer.Typer(help="Database migration commands (Alembic-backed)")
app.add_typer(db_app, name="db")


@db_app.command("migrate")
def db_migrate_alembic(
    revision: str = typer.Option("head", "--revision", "-r", help="Target revision (default: head)"),
) -> None:
    """Apply pending Alembic migrations (equivalent to `alembic upgrade head`)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=str(Path(__file__).resolve().parent),
    )
    raise typer.Exit(result.returncode)


@db_app.command("rollback")
def db_rollback(
    revision: str = typer.Option("-1", "--revision", "-r", help="Target revision (default: -1, one step back)"),
) -> None:
    """Roll back the most recent Alembic migration (equivalent to `alembic downgrade -1`)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", revision],
        cwd=str(Path(__file__).resolve().parent),
    )
    raise typer.Exit(result.returncode)


if __name__ == "__main__":
    app()
