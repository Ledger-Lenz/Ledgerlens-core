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
    calibrate: bool = typer.Option(True, "--calibrate/--no-calibrate", help="Run conformal calibration after training"),
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

    results = train_ensemble(df, calibrate=calibrate)
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
    model_names = [k for k in new_results if k != "_calib"]
    for name in model_names:
        result = new_results[name]
        logger.info("New %s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f", name, result["auc_roc"], result["pr_auc"], result["f1"])

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
) -> None:
    """Stream trades from Horizon SSE and score wallets in near-real-time."""
    import run_pipeline

    run_pipeline.run_streaming(batch_size=batch_size, flush_interval_seconds=flush_interval)


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


@app.command("webhook-worker")
def webhook_worker(
    interval: float = typer.Option(5.0, "--interval", help="Poll interval in seconds"),
) -> None:
    """Run the webhook delivery worker as a foreground process."""
    import asyncio

    from detection.webhook_worker import run_delivery_worker

    asyncio.run(run_delivery_worker(interval_seconds=interval))


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


config_app = typer.Typer(help="Configuration commands")
app.add_typer(config_app, name="config")


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


if __name__ == "__main__":
    app()
