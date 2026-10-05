"""MLflow experiment tracking helpers for model training runs.

Provides ``mlflow_run`` context manager that wraps training with a single
MLflow run, logging hyperparameters, training/validation metrics, training
duration, dataset hash, and model artifacts.

Write-through to model_registry (Issue #935)
---------------------------------------------
``mlflow_tracker`` is NOT an independent state store for model promotion/serving
state.  ``detection.model_registry`` is the single source of truth.

After any state-changing MLflow event (run completion, model registration),
:func:`sync_mlflow_run_to_registry` writes the MLflow run metadata back to
``training_metadata.json`` so both systems stay in sync.

A background consistency-check job (:func:`run_consistency_check`) can be
scheduled (e.g. via APScheduler or a cron job) to alert on any divergence.

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
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Optional dependency: mlflow  (pip install 'ledgerlens-core[ml]')
# ---------------------------------------------------------------------------
try:
    import mlflow
    _HAS_MLFLOW = True
except ImportError:  # pragma: no cover
    mlflow = None  # type: ignore[assignment]
    _HAS_MLFLOW = False

import pandas as pd

from config.settings import settings

if TYPE_CHECKING:
    pass  # reserved for future type-only imports

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
    """Return a short SHA-256 hex digest of the DataFrame schema and content."""
    schema = f"{len(df)}|{','.join(sorted(df.columns))}"
    content = df.to_json(orient="values")
    raw = schema + "|" + content
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


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


def log_hyperparameters(params: dict) -> None:
    """Log a dict of hyperparameters as MLflow params (flattened)."""
    for key, value in params.items():
        try:
            mlflow.log_param(key, value)
        except Exception as exc:
            logger.warning("Failed to log param %s=%s: %s", key, value, exc)


def log_metrics(metrics: dict, step: int | None = None) -> None:
    """Log a dict of metrics to the current MLflow run."""
    for key, value in metrics.items():
        try:
            mlflow.log_metric(key, value, step=step)
        except Exception as exc:
            logger.warning("Failed to log metric %s=%s: %s", key, value, exc)


# ---------------------------------------------------------------------------
# Write-through sync helpers (Issue #935)
# ---------------------------------------------------------------------------
# model_registry.py is the single source of truth for promotion/serving state.
# These helpers write MLflow run metadata into training_metadata.json so both
# systems remain consistent.  Call sync_mlflow_run_to_registry() immediately
# after a run completes.
# ---------------------------------------------------------------------------


def sync_mlflow_run_to_registry(
    run_id: str,
    model_dir: str | None = None,
    tracking_uri: str | None = None,
) -> None:
    """Write MLflow run metadata to the registry's training_metadata.json.

    Pulls the run's params, metrics, and tags from MLflow and writes them into
    ``{model_dir}/training_metadata.json`` under the ``"mlflow"`` key, making
    the registry the authoritative view of every MLflow run that touched this
    model directory.

    This is a **write-through** operation: it must be called at the end of
    every training run.  The ``mlflow_run`` context manager calls it
    automatically when ``sync_to_registry=True`` (the default).

    Args:
        run_id: MLflow run ID to sync.
        model_dir: Target model directory.  Defaults to ``settings.model_dir``.
        tracking_uri: MLflow tracking URI.  Defaults to resolved tracking URI.
    """
    if not _HAS_MLFLOW:
        logger.warning("mlflow not installed; skipping write-through sync to registry")
        return

    actual_model_dir = model_dir or settings.model_dir
    uri = _resolve_tracking_uri(tracking_uri)
    mlflow.set_tracking_uri(uri)

    try:
        client = mlflow.tracking.MlflowClient()
        run = client.get_run(run_id)
    except Exception as exc:
        logger.warning("Could not fetch MLflow run %s for registry sync: %s", run_id, exc)
        return

    run_data = {
        "run_id": run_id,
        "status": run.info.status,
        "start_time": run.info.start_time,
        "end_time": run.info.end_time,
        "params": dict(run.data.params),
        "metrics": {k: v for k, v in run.data.metrics.items()},
        "tags": dict(run.data.tags),
        "synced_at": time.time(),
    }

    metadata_path = os.path.join(actual_model_dir, "training_metadata.json")
    metadata: dict = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing metadata for sync: %s", exc)

    mlflow_runs: dict = metadata.setdefault("mlflow_runs", {})
    mlflow_runs[run_id] = run_data
    # Always keep the most-recent run_id at the top level for quick access
    metadata["mlflow_run_id"] = run_id

    os.makedirs(actual_model_dir, exist_ok=True)
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info(
        "Write-through sync: MLflow run %s -> %s/training_metadata.json",
        run_id, actual_model_dir,
    )


@contextlib.contextmanager
def mlflow_run(
    experiment_name: str | None = None,
    tracking_uri: str | None = None,
    nested: bool = False,
    parent_run_id: str | None = None,
) -> Generator[str, None, None]:
    """Context manager that creates (or reuses) an MLflow run.

    Sets the tracking URI, creates/gets the experiment by name, and starts
    a run. Yields the ``run_id`` so callers can log additional params, metrics
    or artifacts inside the ``with`` block.

    When *nested* is ``True``, starts a nested run inside an existing active
    run (useful for per-model sub-runs inside the ensemble loop).

    When *sync_to_registry* is ``True`` (default, Issue #935), calls
    :func:`sync_mlflow_run_to_registry` on exit so ``training_metadata.json``
    stays consistent with MLflow state.

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
    sync_to_registry:
        If ``True`` (default), write MLflow run metadata to registry on exit.
    model_dir:
        Model directory for write-through sync.  Defaults to ``settings.model_dir``.

    parent_run_id:
        Optional MLflow parent run ID for a new run resumed from a training
        checkpoint.
    Yields
    ------
    str
        The MLflow ``run_id`` of the started (or resumed) run.
    """
    if not _HAS_MLFLOW:
        raise ImportError(
            "'mlflow' is required by detection/mlflow_tracker.py but is not installed.\n"
            "  Install the 'ml' extra:  pip install 'ledgerlens-core[ml]'\n"
            "  Or install directly:     pip install mlflow"
        )
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

    tags = {"mlflow.parentRunId": parent_run_id} if parent_run_id else None
    run = mlflow.start_run(
        experiment_id=exp.experiment_id,
        nested=nested,
        tags=tags,
    )
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
        mlflow.log_metric("training_duration_seconds", elapsed)
        mlflow.end_run(status="FINISHED")
        logger.info("Finished MLflow run %s (%.2f s)", run_id, elapsed)
        if sync_to_registry and run_id:
            try:
                sync_mlflow_run_to_registry(run_id, model_dir=model_dir, tracking_uri=uri)
            except Exception as exc:
                logger.warning("Write-through sync failed for run %s: %s", run_id, exc)


# ---------------------------------------------------------------------------
# Consistency-check job (Issue #935)
# ---------------------------------------------------------------------------


class RegistryDivergenceError(RuntimeError):
    """Raised by run_consistency_check when MLflow and registry states diverge."""


def run_consistency_check(
    model_dir: str | None = None,
    tracking_uri: str | None = None,
    alert: bool = True,
) -> dict:
    """Compare MLflow run records with the registry's training_metadata.json.

    Checks that:
    1. ``training_metadata.json`` has a ``mlflow_run_id`` entry.
    2. The run exists and is FINISHED in MLflow.
    3. The run_id recorded in ``training_metadata.json`` matches the latest
       MLflow run for the experiment.

    Returns a dict with keys:
      - ``"consistent"`` (bool)
      - ``"run_id_registry"`` — run_id from training_metadata.json
      - ``"run_id_mlflow"`` — latest run_id from MLflow experiment
      - ``"divergences"`` — list of human-readable divergence descriptions

    When *alert* is ``True`` and divergences are found, logs at ERROR level.
    Set *alert* to ``False`` in tests to suppress log noise.

    Raises :class:`RegistryDivergenceError` when the check itself cannot be
    completed (e.g. MLflow is unreachable) and divergence cannot be determined.
    """
    if not _HAS_MLFLOW:
        logger.warning("mlflow not installed; skipping consistency check")
        return {"consistent": True, "divergences": [], "skipped": True}

    actual_model_dir = model_dir or settings.model_dir
    uri = _resolve_tracking_uri(tracking_uri)

    # --- Load registry state ---
    metadata_path = os.path.join(actual_model_dir, "training_metadata.json")
    registry_run_id: str | None = None
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                meta = json.load(f)
            registry_run_id = meta.get("mlflow_run_id")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read training_metadata.json: %s", exc)

    # --- Load MLflow state ---
    mlflow.set_tracking_uri(uri)
    exp_name = settings.mlflow_experiment_name or "ledgerlens-training"
    mlflow_run_id: str | None = None
    try:
        client = mlflow.tracking.MlflowClient()
        experiment = client.get_experiment_by_name(exp_name)
        if experiment is not None:
            runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                order_by=["start_time DESC"],
                max_results=1,
            )
            if runs:
                mlflow_run_id = runs[0].info.run_id
    except Exception as exc:
        raise RegistryDivergenceError(
            f"Could not query MLflow to complete consistency check: {exc}"
        ) from exc

def log_checkpoint_metadata(
    metadata: dict,
    *,
    checkpoint_path: str,
    epoch: int,
    step: int,
) -> None:
    """Log checkpoint provenance and resume lineage to the active MLflow run."""
    if not _HAS_MLFLOW:
        return

    checkpoint_id = Path(checkpoint_path).name
    artifact_metadata = {
        **metadata,
        "checkpoint_identifier": checkpoint_id,
        "checkpoint_path": str(checkpoint_path),
        "progress_epoch": epoch,
        "progress_step": step,
    }
    tags = {
        "training.latest_checkpoint": str(checkpoint_path),
        "training.resumed": str(bool(metadata.get("resumed", False))).lower(),
    }
    original_run_id = metadata.get("original_mlflow_run_id")
    if original_run_id:
        tags["training.original_run_id"] = str(original_run_id)

    try:
        mlflow.log_dict(artifact_metadata, f"checkpoints/{checkpoint_id}.json")
        mlflow.set_tags(tags)
    except Exception as exc:
        logger.warning("Failed to log checkpoint metadata for %s: %s", checkpoint_path, exc)


def log_artifact(path: str) -> None:
    """Log a local file to the active MLflow run when tracking is available."""
    if not _HAS_MLFLOW:
        return
    try:
        mlflow.log_artifact(path)
    except Exception as exc:
        logger.warning("Failed to log MLflow artifact %s: %s", path, exc)


def log_training_dataset_metadata(df: pd.DataFrame) -> None:
    """Log dataset shape, column count, label distribution, and a content hash."""
    mlflow.log_param("dataset_rows", len(df))
    mlflow.log_param("dataset_columns", len(df.columns))
    mlflow.log_param("dataset_hash", _compute_dataset_hash(df))

    if registry_run_id is None:
        divergences.append(
            "training_metadata.json has no mlflow_run_id — registry may never have been synced"
        )
    if mlflow_run_id is None:
        divergences.append(
            f"No MLflow runs found for experiment '{exp_name}'"
        )
    if registry_run_id and mlflow_run_id and registry_run_id != mlflow_run_id:
        divergences.append(
            f"run_id mismatch: registry has {registry_run_id!r}, "
            f"MLflow latest is {mlflow_run_id!r}. "
            "Call sync_mlflow_run_to_registry() to reconcile."
        )

    result = {
        "consistent": len(divergences) == 0,
        "run_id_registry": registry_run_id,
        "run_id_mlflow": mlflow_run_id,
        "divergences": divergences,
    }

    if divergences and alert:
        for msg in divergences:
            logger.error("[consistency-check] Divergence detected: %s", msg)

    return result
