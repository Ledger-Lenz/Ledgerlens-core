"""Admin REST API for model lifecycle and system configuration (Issue #160)."""

import glob
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.auth import require_admin_key
from config.settings import settings, _runtime_cache
from detection.model_registry import get_current_version, list_model_versions
from detection.storage import get_krum_aggregation_log

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])

_MODEL_NAMES = ["random_forest", "xgboost", "lightgbm"]

# Rate limiter instance for the reset endpoint
_limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# GET /admin/models
# ---------------------------------------------------------------------------


@router.get("/models", include_in_schema=False)
def list_models() -> list[dict]:
    """List all versioned model files with active/inactive deployment status."""
    model_dir = settings.model_dir
    result: dict[str, dict] = {}

    for name in _MODEL_NAMES:
        current = get_current_version(name, model_dir)
        try:
            versions = list_model_versions(name, model_dir)
        except (FileNotFoundError, OSError):
            versions = []
        for v in versions:
            key = v
            if key not in result:
                result[key] = {"version": v, "models": [], "active": v == current}
            result[key]["models"].append(name)
            if v == current:
                result[key]["active"] = True

    return list(result.values())


# ---------------------------------------------------------------------------
# POST /admin/models/{version}/promote
# ---------------------------------------------------------------------------


@router.post("/models/{version}/promote", include_in_schema=False)
def promote_model(version: str) -> dict:
    """Promote ``version`` to active for all three model types."""
    model_dir = settings.model_dir
    missing = [
        name
        for name in _MODEL_NAMES
        if not os.path.isfile(os.path.join(model_dir, f"{name}_v{version}.joblib"))
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Model files not found for version {version!r}: {missing}",
        )

    for name in _MODEL_NAMES:
        latest_path = os.path.join(model_dir, f"{name}_latest.txt")
        with open(latest_path, "w") as f:
            f.write(version)

    return {"promoted": version, "models": _MODEL_NAMES}


# ---------------------------------------------------------------------------
# GET /admin/config
# ---------------------------------------------------------------------------


@router.get("/config", include_in_schema=False)
def get_config() -> dict:
    """Return the current runtime configuration from the `runtime_config` table."""
    config: dict = {}
    try:
        with sqlite3.connect(settings.db_path) as conn:
            for key, value in conn.execute("SELECT key, value FROM runtime_config"):
                config[key] = value
    except sqlite3.OperationalError:
        pass
    return config


# ---------------------------------------------------------------------------
# PATCH /admin/config
# ---------------------------------------------------------------------------


class RuntimeConfigPatch(BaseModel):
    updates: dict[str, str]


@router.patch("/config", include_in_schema=False)
def patch_config(body: RuntimeConfigPatch) -> dict:
    """Persist config key/value updates to SQLite and invalidate the in-process cache."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        for key, value in body.updates.items():
            conn.execute(
                "INSERT INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )

    # Invalidate the in-process cache so next load_runtime_config() re-reads from DB
    _runtime_cache["ts"] = 0
    _runtime_cache["config"] = {}

    return {"updated": list(body.updates.keys())}


# ---------------------------------------------------------------------------
# POST /admin/retrain
# ---------------------------------------------------------------------------


def _ensure_retrain_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS retrain_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT
        )"""
    )


def _run_retrain(job_id: str) -> None:
    """Background task: run retraining and update job status in SQLite."""
    started_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        _ensure_retrain_jobs_table(conn)
        conn.execute(
            "INSERT INTO retrain_jobs (job_id, status, started_at) VALUES (?, ?, ?)",
            (job_id, "running", started_at),
        )

    try:
        from detection.model_training import train_models
        from ingestion.synthetic_data import generate_synthetic_trades

        trades = generate_synthetic_trades()
        train_models(trades, model_dir=settings.model_dir)
        status = "completed"
    except Exception:
        status = "failed"

    completed_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        _ensure_retrain_jobs_table(conn)
        conn.execute(
            "UPDATE retrain_jobs SET status=?, completed_at=? WHERE job_id=?",
            (status, completed_at, job_id),
        )


@router.post("/retrain", include_in_schema=False)
def trigger_retrain(background_tasks: BackgroundTasks) -> dict:
    """Enqueue an async retraining job and return its job ID."""
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_retrain, job_id)
    return {"job_id": job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# FL Privacy endpoint  (Issue #145)
# ---------------------------------------------------------------------------

import logging as _logging
_logger = _logging.getLogger("ledgerlens.admin")


class FLPrivacyStatus(BaseModel):
    current_epsilon: float
    target_epsilon: float
    delta: float
    noise_multiplier: float
    clip_norm: float
    budget_exhausted: bool
    rounds_completed: int


@router.get("/fl/privacy", response_model=FLPrivacyStatus, include_in_schema=False)
def fl_privacy_status() -> FLPrivacyStatus:
    """Return current FL differential privacy budget status (admin-key gated)."""
    db_path = settings.db_path
    try:
        from detection.federated.privacy_utils import get_privacy_log
        rows = get_privacy_log(db_path)
    except Exception:
        rows = []

    target_epsilon = float(os.environ.get("FL_DP_TARGET_EPSILON", "1.0"))
    delta = float(os.environ.get("FL_DP_DELTA", "1e-5"))
    noise_multiplier = float(os.environ.get("FL_DP_NOISE_MULTIPLIER", "0.0")) or float(
        getattr(settings, "federated_noise_multiplier", 0.0)
    )
    clip_norm = float(os.environ.get("FL_DP_CLIP_NORM", "1.0"))
    current_epsilon = rows[-1]["epsilon"] if rows else 0.0
    budget_exhausted = current_epsilon >= target_epsilon

    return FLPrivacyStatus(
        current_epsilon=current_epsilon,
        target_epsilon=target_epsilon,
        delta=delta,
        noise_multiplier=noise_multiplier,
        clip_norm=clip_norm,
        budget_exhausted=budget_exhausted,
        rounds_completed=len(rows),
    )
