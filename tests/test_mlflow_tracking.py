"""Integration tests for MLflow experiment tracking during model training.

Verifies that ``train_ensemble`` creates an MLflow run with the correct
parameter keys, metric keys, and model artifacts.
"""

import json
import os

import mlflow
import pandas as pd
import pytest

from detection.feature_engineering import FEATURE_NAMES
from detection.mlflow_tracker import (
    _compute_dataset_hash,
    log_checkpoint_metadata,
    mlflow_run,
)
from detection.model_training import train_ensemble


@pytest.fixture
def tmp_mlruns(tmp_path):
    """Yield a temporary directory to use as the MLflow tracking URI."""
    uri = tmp_path / "mlruns"
    uri.mkdir()
    yield uri.as_posix()


@pytest.fixture
def mini_dataset():
    """A tiny labelled dataset sufficient to train the ensemble."""
    rng = pd.array = __import__("numpy").random.default_rng(42)

    n = 200
    data = {}
    for name in FEATURE_NAMES:
        data[name] = rng.uniform(-1, 1, n).tolist()
    data["label"] = [1 if i < n // 2 else 0 for i in range(n)]
    return pd.DataFrame(data)


def test_mlflow_run_creates_experiment_and_run(tmp_mlruns):
    """A basic ``mlflow_run`` context creates a run with correct metadata."""
    with mlflow_run(
        experiment_name="test-experiment",
        tracking_uri=tmp_mlruns,
    ) as run_id:
        assert run_id, "mlflow_run should yield a non-empty run_id"

    client = mlflow.tracking.MlflowClient(tmp_mlruns)
    run = client.get_run(run_id)
    assert run.info.run_id == run_id
    assert run.info.experiment_id is not None


def test_mlflow_run_logs_training_duration(tmp_mlruns):
    """The context manager logs ``training_duration_seconds``."""
    with mlflow_run(
        experiment_name="test-duration",
        tracking_uri=tmp_mlruns,
    ):
        pass

    client = mlflow.tracking.MlflowClient(tmp_mlruns)
    experiment = client.get_experiment_by_name("test-duration")
    assert experiment is not None, "experiment 'test-duration' should exist"
    runs = client.search_runs(experiment_ids=[experiment.experiment_id])
    durations = [r.data.metrics.get("training_duration_seconds", 0.0) for r in runs if r.data.metrics]
    assert any(d > 0.0 for d in durations)


def test_checkpoint_metadata_preserves_run_lineage(tmp_mlruns, tmp_path):
    with mlflow_run(
        experiment_name="checkpoint-lineage", tracking_uri=tmp_mlruns
    ) as parent_id:
        pass

    checkpoint_path = tmp_path / "lstm_epoch_0002.pt"
    checkpoint_path.touch()
    with mlflow_run(
        experiment_name="checkpoint-lineage",
        tracking_uri=tmp_mlruns,
        parent_run_id=parent_id,
    ) as resumed_id:
        log_checkpoint_metadata(
            {
                "original_mlflow_run_id": parent_id,
                "current_mlflow_run_id": resumed_id,
                "resumed": True,
            },
            checkpoint_path=str(checkpoint_path),
            epoch=2,
            step=17,
        )

    client = mlflow.tracking.MlflowClient(tmp_mlruns)
    resumed_run = client.get_run(resumed_id)
    assert resumed_run.data.tags["mlflow.parentRunId"] == parent_id
    assert resumed_run.data.tags["training.original_run_id"] == parent_id
    assert resumed_run.data.tags["training.resumed"] == "true"

    artifact_path = client.download_artifacts(
        resumed_id, "checkpoints/lstm_epoch_0002.pt.json"
    )
    with open(artifact_path, encoding="utf-8") as artifact_file:
        metadata = json.load(artifact_file)
    assert metadata["checkpoint_identifier"] == checkpoint_path.name
    assert metadata["checkpoint_path"] == str(checkpoint_path)
    assert metadata["progress_epoch"] == 2
    assert metadata["progress_step"] == 17


def test_compute_dataset_hash_stable():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    h1 = _compute_dataset_hash(df)
    h2 = _compute_dataset_hash(df.copy())
    assert h1 == h2
    assert len(h1) == 12


def test_compute_dataset_hash_changes_with_data():
    df1 = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    df2 = pd.DataFrame({"a": [1, 2], "b": [3, 5]})
    assert _compute_dataset_hash(df1) != _compute_dataset_hash(df2)


def test_train_ensemble_creates_mlflow_run(tmp_mlruns, mini_dataset):
    """``train_ensemble`` creates an MLflow run with correct parameter and metric keys."""
    train_ensemble(
        mini_dataset,
        calibrate=False,
        adversarial_augment=False,
        experiment_name="test-ensemble",
        tracking_uri=tmp_mlruns,
    )

    client = mlflow.tracking.MlflowClient(tmp_mlruns)
    experiment = client.get_experiment_by_name("test-ensemble")
    assert experiment is not None

    runs = client.search_runs(experiment_ids=[experiment.experiment_id])
    assert len(runs) >= 1

    run = runs[0]

    # Core hyperparameters must be present
    expected_params = {
        "random_state",
        "adversarial_augment",
        "calibrate",
        "dataset_rows",
        "dataset_hash",
        "test_split_ratio",
    }
    assert expected_params.issubset(run.data.params.keys()), (
        f"Missing params: {expected_params - set(run.data.params.keys())}"
    )

    # Metrics for each ensemble component
    for prefix in ("random_forest", "xgboost", "lightgbm"):
        for metric in ("auc_roc", "pr_auc", "f1", "precision", "recall"):
            key = f"{prefix}_{metric}"
            assert key in run.data.metrics, f"Missing metric: {key}"

    # Aggregate metrics
    for metric in ("avg_auc_roc", "avg_pr_auc", "avg_f1"):
        assert metric in run.data.metrics, f"Missing aggregate metric: {metric}"

    # Training duration
    assert "training_duration_seconds" in run.data.metrics

    # Model artifacts must exist
    for name in ("random_forest", "xgboost", "lightgbm"):
        artifact_uri = run.info.artifact_uri
        model_path = os.path.join(artifact_uri.replace("file://", ""), name)
        assert os.path.isdir(model_path), f"Missing artifact directory: {model_path}"


def test_train_ensemble_skips_mlflow_when_no_uri(mini_dataset):
    """``train_ensemble`` runs without error when MLflow is not configured."""
    results = train_ensemble(
        mini_dataset,
        calibrate=False,
        adversarial_augment=False,
    )
    assert "random_forest" in results
    assert "xgboost" in results
    assert "lightgbm" in results
