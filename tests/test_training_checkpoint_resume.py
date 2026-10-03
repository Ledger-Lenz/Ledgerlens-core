"""Subprocess coverage for interrupted and resumed training runs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from scripts.train_gnn import _read_gnn_training_checkpoint
from scripts.train_lstm_autoencoder import _read_lstm_training_checkpoint

ROOT = Path(__file__).resolve().parents[1]


def _create_lstm_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE feature_distribution_snapshots (
                wallet TEXT,
                feature_name TEXT,
                feature_value REAL,
                recorded_at TEXT
            )"""
        )
        rows = []
        for wallet_index in range(3):
            for sequence_index in range(4):
                wallet = f"wallet-{wallet_index}"
                recorded_at = f"2026-01-01T00:{sequence_index:02d}:00"
                rows.extend(
                    (
                        wallet,
                        feature_name,
                        value,
                        recorded_at,
                    )
                    for feature_name, value in (
                        ("log_amount_bin", 0.5 + sequence_index),
                        ("trade_count_bin", 1.0 + sequence_index),
                    )
                )
        connection.executemany(
            "INSERT INTO feature_distribution_snapshots VALUES (?, ?, ?, ?)", rows
        )


def _create_gnn_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE ring_members (wallet TEXT, confirmed INTEGER)")
        connection.execute(
            "CREATE TABLE wallet_scores (wallet TEXT, score REAL, scored_at TEXT)"
        )
        connection.execute("CREATE TABLE alerts (wallet TEXT, created_at TEXT)")
        connection.executemany(
            "INSERT INTO ring_members VALUES (?, 1)",
            [(f"positive-{index}",) for index in range(4)],
        )
        connection.executemany(
            "INSERT INTO wallet_scores VALUES (?, 1, '2099-01-01T00:00:00+00:00')",
            [(f"negative-{index}",) for index in range(12)],
        )


def _environment(tracking_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MLFLOW_TRACKING_URI"] = str(tracking_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _terminate_after_checkpoint(
    command: list[str], env: dict[str, str], checkpoint_path: Path
) -> tuple[str, int]:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output = []
    try:
        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if not line:
                raise AssertionError(
                    "training exited before writing its first interval checkpoint "
                    f"(status={process.poll()}): {''.join(output)}"
                )
            output.append(line)
            if checkpoint_path.name in line:
                break
        process.terminate()
        return_code = process.wait(timeout=15)
        if return_code >= 0:
            raise AssertionError(
                f"training did not terminate from the interrupt signal (status={return_code})"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        if process.stdout is not None:
            process.stdout.close()
    return "".join(output), return_code


def _mlflow_child_for(client, experiment_name: str, original_run_id: str):
    experiment = client.get_experiment_by_name(experiment_name)
    assert experiment is not None
    children = [
        run
        for run in client.search_runs([experiment.experiment_id])
        if run.info.run_id != original_run_id
        and run.data.tags.get("mlflow.parentRunId") == original_run_id
    ]
    assert children, "resumed run must be a child of the original MLflow run"
    return children[0]


def _run_lstm_command(
    db_path: Path,
    checkpoint_dir: Path,
    model_dir: Path,
    *,
    epochs: int,
    resume_from: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "scripts/train_lstm_autoencoder.py",
        "--db-path",
        str(db_path),
        "--model-dir",
        str(model_dir),
        "--checkpoint-dir",
        str(checkpoint_dir),
        "--checkpoint-every",
        "1",
        "--epochs",
        str(epochs),
        "--hidden-dim",
        "2",
        "--num-layers",
        "1",
        "--dropout",
        "0",
        "--sequence-length",
        "2",
        "--batch-size",
        "4",
        "--val-split",
        "0.2",
        "--patience",
        "100000",
        "--seed",
        "23",
    ]
    if resume_from is not None:
        command.extend(["--resume-from-checkpoint", str(resume_from)])
    return command


@pytest.mark.timeout(120)
def test_lstm_training_interruption_resumes_with_lineage(tmp_path):
    mlflow = pytest.importorskip("mlflow")
    db_path = tmp_path / "lstm-training.sqlite"
    checkpoint_dir = tmp_path / "lstm-checkpoints"
    model_dir = tmp_path / "lstm-models"
    tracking_dir = tmp_path / "lstm-mlruns"
    _create_lstm_database(db_path)
    env = _environment(tracking_dir)

    checkpoint_path = checkpoint_dir / "lstm_autoencoder_epoch_0001.pt"
    first_output, interrupted_status = _terminate_after_checkpoint(
        _run_lstm_command(db_path, checkpoint_dir, model_dir, epochs=100000),
        env,
        checkpoint_path,
    )
    assert interrupted_status < 0
    assert "Epoch   1/100000" in first_output
    assert checkpoint_path.is_file()

    saved_checkpoint = _read_lstm_training_checkpoint(checkpoint_path)
    assert saved_checkpoint["epoch"] == 1
    assert saved_checkpoint["global_step"] > 0
    assert saved_checkpoint["optimizer_state_dict"]["state"]
    original_run_id = saved_checkpoint["original_mlflow_run_id"]
    assert original_run_id == saved_checkpoint["current_mlflow_run_id"]

    client = mlflow.tracking.MlflowClient(str(tracking_dir))
    client.set_terminated(original_run_id, status="KILLED")
    resumed = subprocess.run(
        _run_lstm_command(
            db_path,
            checkpoint_dir,
            model_dir,
            epochs=3,
            resume_from=checkpoint_path,
        ),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    resumed_output = resumed.stdout + resumed.stderr
    assert resumed.returncode == 0, resumed_output
    assert "Resuming LSTM training from epoch 2" in resumed_output
    assert re.search(r"Epoch\s+2/3", resumed_output)
    assert re.search(r"Epoch\s+1/3", resumed_output) is None

    from detection.temporal_patterns import load_lstm_autoencoder

    model = load_lstm_autoencoder(str(model_dir / "lstm_autoencoder.pt"))
    assert model is not None
    assert all(torch.isfinite(parameter).all() for parameter in model.parameters())
    metadata = json.loads((model_dir / "lstm_training_metadata.json").read_text())
    assert metadata["epochs_run"] == 3

    resumed_run = _mlflow_child_for(
        client, "lstm-autoencoder-training", original_run_id
    )
    assert resumed_run.data.tags["training.original_run_id"] == original_run_id
    assert resumed_run.data.tags["training.resumed"] == "true"
    artifact = client.download_artifacts(
        resumed_run.info.run_id,
        "checkpoints/lstm_autoencoder_epoch_0003.pt.json",
    )
    with open(artifact, encoding="utf-8") as artifact_file:
        checkpoint_metadata = json.load(artifact_file)
    assert checkpoint_metadata["original_mlflow_run_id"] == original_run_id
    assert checkpoint_metadata["current_mlflow_run_id"] == resumed_run.info.run_id
    assert checkpoint_metadata["resumed"] is True
    assert checkpoint_metadata["progress_epoch"] == 3
    assert checkpoint_metadata["progress_step"] > saved_checkpoint["global_step"]


def _run_gnn_command(
    db_path: Path,
    checkpoint_dir: Path,
    model_path: Path,
    *,
    epochs: int,
    resume_from: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "scripts/train_gnn.py",
        "--db-path",
        str(db_path),
        "--model-path",
        str(model_path),
        "--checkpoint-dir",
        str(checkpoint_dir),
        "--checkpoint-every",
        "1",
        "--epochs",
        str(epochs),
        "--patience",
        "100000",
        "--hidden-channels",
        "4",
        "--out-channels",
        "2",
        "--num-layers",
        "1",
        "--dropout",
        "0",
        "--seed",
        "23",
    ]
    if resume_from is not None:
        command.extend(["--resume-from-checkpoint", str(resume_from)])
    return command


@pytest.mark.timeout(120)
def test_gnn_training_interruption_resumes_with_lineage(tmp_path):
    pytest.importorskip("torch_geometric")
    mlflow = pytest.importorskip("mlflow")
    db_path = tmp_path / "gnn-training.sqlite"
    checkpoint_dir = tmp_path / "gnn-checkpoints"
    model_path = tmp_path / "gnn-models" / "gnn_ring_detector.pt"
    tracking_dir = tmp_path / "gnn-mlruns"
    _create_gnn_database(db_path)
    env = _environment(tracking_dir)

    checkpoint_path = checkpoint_dir / "gnn_ring_detector_epoch_0001.pt"
    first_output, interrupted_status = _terminate_after_checkpoint(
        _run_gnn_command(db_path, checkpoint_dir, model_path, epochs=100000),
        env,
        checkpoint_path,
    )
    assert interrupted_status < 0
    assert "Epoch   1/100000" in first_output
    assert checkpoint_path.is_file()

    saved_checkpoint = _read_gnn_training_checkpoint(checkpoint_path)
    assert saved_checkpoint["epoch"] == 1
    assert saved_checkpoint["global_step"] == 1
    assert saved_checkpoint["optimizer_state_dict"]["state"]
    original_run_id = saved_checkpoint["original_mlflow_run_id"]
    assert original_run_id == saved_checkpoint["current_mlflow_run_id"]

    client = mlflow.tracking.MlflowClient(str(tracking_dir))
    client.set_terminated(original_run_id, status="KILLED")
    resumed = subprocess.run(
        _run_gnn_command(
            db_path,
            checkpoint_dir,
            model_path,
            epochs=3,
            resume_from=checkpoint_path,
        ),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    resumed_output = resumed.stdout + resumed.stderr
    assert resumed.returncode == 0, resumed_output
    assert "Resuming GNN training from epoch 2" in resumed_output

    final_checkpoint = _read_gnn_training_checkpoint(
        checkpoint_dir / "gnn_ring_detector_epoch_0003.pt"
    )
    assert final_checkpoint["epoch"] == 3
    assert final_checkpoint["global_step"] == 3
    assert final_checkpoint["resumed"] is True
    assert final_checkpoint["original_mlflow_run_id"] == original_run_id

    from detection.gnn_ring_detector import GNNRingDetector

    detector = GNNRingDetector(
        model_path=str(model_path),
        fallback_to_scc=False,
        hidden_channels=4,
        out_channels=2,
        num_layers=1,
        dropout=0,
    )
    detector.load()
    assert detector._fitted
    assert model_path.with_suffix(".sha256").is_file()

    resumed_run = _mlflow_child_for(client, "gnn-ring-detector-training", original_run_id)
    assert resumed_run.data.tags["training.original_run_id"] == original_run_id
    assert resumed_run.data.tags["training.resumed"] == "true"
    artifact = client.download_artifacts(
        resumed_run.info.run_id,
        "checkpoints/gnn_ring_detector_epoch_0003.pt.json",
    )
    with open(artifact, encoding="utf-8") as artifact_file:
        checkpoint_metadata = json.load(artifact_file)
    assert checkpoint_metadata["original_mlflow_run_id"] == original_run_id
    assert checkpoint_metadata["current_mlflow_run_id"] == resumed_run.info.run_id
    assert checkpoint_metadata["resumed"] is True
    assert checkpoint_metadata["progress_epoch"] == 3
    assert checkpoint_metadata["progress_step"] == 3


def test_invalid_training_checkpoint_is_rejected(tmp_path):
    import torch

    checkpoint_path = tmp_path / "invalid.pt"
    torch.save({"not": "a training checkpoint"}, checkpoint_path)

    with pytest.raises(ValueError, match="Invalid LSTM training checkpoint"):
        _read_lstm_training_checkpoint(checkpoint_path)
    with pytest.raises(ValueError, match="Invalid GNN training checkpoint"):
        _read_gnn_training_checkpoint(checkpoint_path)
