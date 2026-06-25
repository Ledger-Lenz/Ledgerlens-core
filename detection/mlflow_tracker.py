"""MLflow experiment tracking helpers for model training runs.

Provides ``mlflow_run`` context manager that wraps training with a single
MLflow run, logging hyperparameters, training/validation metrics, training
duration, dataset hash, and model artifacts.

Usage::

    with mlflow_run(experiment_name="benford-v2") as run_id:
        # training code
        mlflow.log_param("n_estimators", 200)
        mlflow.log_metric("auc_roc", 0.95)
        mlflow.sklearn.log_model(model, "random_forest")
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from typing import TYPE_CHECKING

import mlflow
import pandas as pd

from config.settings import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger("ledgerlens.mlflow_tracker")


def _resolve_tracking_uri(uri: str | None) -> str:
    """Return the effective MLflow tracking URI.

    Precedence: explicit argument > ``MLFLOW_TRACKING_URI`` env var >
    ``settings.mlflow_tracking_uri`` > ``./mlruns``.
    """
    if uri is not None:
        return uri
    import os as _os
    env = _os.getenv("MLFLOW_TRACKING_URI")
    if env:
        return env
    return settings.mlflow_tracking_uri


def _compute_dataset_hash(df: pd.DataFrame) -> str:
    """Return a short SHA-256 hex digest of the DataFrame columns + row count."""
    raw = f"{len(df)}|{','.join(sorted(df.columns))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


@contextlib.contextmanager
def mlflow_run(
    experiment_name: str | None = None,
    tracking_uri: str | None = None,
    nested: bool = False,
) -> Generator[str, None, None]:
    """Context manager that creates (or reuses) an MLflow run.

    Sets the tracking URI, creates/gets the experiment by name, and starts
    a run. Yields the ``run_id`` so callers can log additional params, metrics
    or artifacts inside the ``with`` block.

    When *nested* is ``True``, starts a nested run inside an existing active
    run (useful for per-model sub-runs inside the ensemble loop).

    Parameters
    ----------
    experiment_name:
        MLflow experiment name.  Falls back to ``settings.mlflow_experiment_name``
        then ``"ledgerlens-training"``.
    tracking_uri:
        MLflow tracking URI.  Falls back to ``MLFLOW_TRACKING_URI`` env var,
        then ``settings.mlflow_tracking_uri``, then ``./mlruns``.
    nested:
        If ``True``, start a nested (child) run inside an already-active run.

    Yields
    ------
    str
        The MLflow ``run_id`` of the started (or resumed) run.
    """
    uri = _resolve_tracking_uri(tracking_uri)
    exp_name = experiment_name or settings.mlflow_experiment_name or "ledgerlens-training"

    mlflow.set_tracking_uri(uri)

    try:
        exp = mlflow.set_experiment(exp_name)
        logger.info("MLflow experiment: %s (id=%s, tracking_uri=%s)", exp_name, exp.experiment_id, uri)
    except Exception as exc:
        logger.warning("Failed to set MLflow experiment %s: %s — skipping MLflow tracking", exp_name, exc)
        yield ""
        return

    run = mlflow.start_run(experiment_id=exp.experiment_id, nested=nested)
    run_id = run.info.run_id
    logger.info("Started MLflow run: %s", run_id)

    start_time = time.monotonic()
    try:
        yield run_id
    except Exception:
        logger.exception("MLflow run %s failed — logging exception", run_id)
        mlflow.log_param("status", "failed")
        raise
    finally:
        elapsed = time.monotonic() - start_time
        mlflow.log_metric("training_duration_seconds", round(elapsed, 2))
        mlflow.end_run(status="FINISHED")
        logger.info("Finished MLflow run %s (%.2f s)", run_id, elapsed)


def log_hyperparameters(params: dict) -> None:
    """Log a dict of hyperparameters as MLflow params (flattened)."""
    for key, value in params.items():
        try:
            mlflow.log_param(key, value)
        except Exception as exc:
            logger.debug("Failed to log param %s=%s: %s", key, value, exc)


def log_metrics(metrics: dict, step: int | None = None) -> None:
    """Log a dict of metrics to the current MLflow run."""
    for key, value in metrics.items():
        try:
            mlflow.log_metric(key, value, step=step)
        except Exception as exc:
            logger.debug("Failed to log metric %s=%s: %s", key, value, exc)


def log_training_dataset_metadata(df: pd.DataFrame) -> None:
    """Log dataset shape, column count, label distribution, and a content hash."""
    mlflow.log_param("dataset_rows", len(df))
    mlflow.log_param("dataset_columns", len(df.columns))
    mlflow.log_param("dataset_hash", _compute_dataset_hash(df))

    if "label" in df.columns:
        pos = int(df["label"].sum())
        neg = len(df) - pos
        mlflow.log_param("label_pos_count", pos)
        mlflow.log_param("label_neg_count", neg)
        mlflow.log_param("label_pos_ratio", round(pos / len(df), 6) if len(df) else 0.0)
