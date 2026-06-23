"""Tests for model_training with conformal calibration."""

import json
import os


def test_training_with_calibration_creates_metrics(tmp_path):
    """After training with --calibrate, metrics.json must contain
    conformal_empirical_coverage."""
    from detection.dataset import build_training_dataset
    from detection.model_training import save_models, train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=30, n_wash_rings=5, ring_size=3, seed=42
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)

    results = train_ensemble(df, calibrate=True)

    # Verify calibration key exists in results
    assert "_calib" in results, "Expected '_calib' key in results when calibrate=True"
    calib = results["_calib"]
    assert "calibrators" in calib, "Expected 'calibrators' in calibration data"
    assert len(calib["calibrators"]) == 3, "Expected calibrators for all 3 models"

    # Save models and check metrics.json
    model_dir = str(tmp_path)
    save_models(results, model_dir=model_dir)

    metrics_path = os.path.join(model_dir, "metrics.json")
    assert os.path.exists(metrics_path), f"Expected metrics.json at {metrics_path}"

    with open(metrics_path, "r") as f:
        metrics = json.load(f)

    assert "conformal_empirical_coverage" in metrics, (
        "Expected conformal_empirical_coverage in metrics.json"
    )
    coverage = metrics["conformal_empirical_coverage"]
    assert isinstance(coverage, float), f"Expected float coverage, got {type(coverage)}"
    assert 0.0 <= coverage <= 1.0, f"Coverage must be in [0, 1], got {coverage}"

    # Check per-model coverage keys
    for model_name in ("random_forest", "xgboost", "lightgbm"):
        key = f"conformal_empirical_coverage_{model_name}"
        assert key in metrics, f"Expected {key} in metrics.json"

    # Check calibration index range is logged
    assert "calibration_index_start" in metrics
    assert "calibration_index_end" in metrics


def test_training_without_calibration_skips_metrics(tmp_path):
    """When calibrate=False, no calibration artifacts or metrics should be written."""
    from detection.dataset import build_training_dataset
    from detection.model_training import save_models, train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=30, n_wash_rings=5, ring_size=3, seed=42
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)

    results = train_ensemble(df, calibrate=False)
    assert "_calib" not in results, "Should not have calibration data when calibrate=False"

    model_dir = str(tmp_path)
    save_models(results, model_dir=model_dir)

    # metrics.json may exist but shouldn't have conformal keys
    metrics_path = os.path.join(model_dir, "metrics.json")
    if os.path.exists(metrics_path):
        with open(metrics_path, "r") as f:
            metrics = json.load(f)
        assert "conformal_empirical_coverage" not in metrics, (
            "Should not have conformal coverage when calibrate=False"
        )


def test_calibration_artifact_files_created(tmp_path):
    """Calibration JSON files must be created for each model."""
    from detection.dataset import build_training_dataset
    from detection.model_training import save_models, train_ensemble
    from ingestion.synthetic_data import generate_synthetic_dataset

    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=30, n_wash_rings=5, ring_size=3, seed=42
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)

    results = train_ensemble(df, calibrate=True)
    model_dir = str(tmp_path)
    save_models(results, model_dir=model_dir)

    for model_name in ("random_forest", "xgboost", "lightgbm"):
        cal_path = os.path.join(model_dir, f"{model_name}_conformal.json")
        assert os.path.exists(cal_path), f"Missing calibration artifact: {cal_path}"

        with open(cal_path, "r") as f:
            payload = json.load(f)
        assert "data" in payload
        assert "sha256" in payload
        assert "q_hat" in payload["data"]
        assert "alpha" in payload["data"]
        assert payload["data"]["alpha"] == 0.10
