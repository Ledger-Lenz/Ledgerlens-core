"""Admin REST API for model lifecycle and system configuration (Issue #160)."""

import glob
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from api.auth import require_admin_key
from api.webhook_sender import list_dlq, get_dlq_entry
from api.webhook_sender import WebhookRetryQueue
from config.settings import settings, _runtime_cache
from detection.model_registry import get_current_version, list_model_versions
from detection.webhook_registry import get_subscriber

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])

_MODEL_NAMES = ["random_forest", "xgboost", "lightgbm"]


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


class ConfigPatch(BaseModel):
    updates: dict[str, str]


@router.patch("/config", include_in_schema=False)
def patch_config(body: ConfigPatch) -> dict:
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
# GET /admin/webhooks/dlq
# ---------------------------------------------------------------------------


@router.get("/webhooks/dlq", include_in_schema=False)
def list_webhook_dlq() -> list[dict]:
    """List all dead-lettered webhook deliveries."""
    entries = list_dlq(db_path=settings.db_path)
    return [
        {
            "id": e.id,
            "subscriber_id": e.subscriber_id,
            "url": e.url,
            "attempt_count": e.attempt_count,
            "last_error": e.last_error,
            "dead_lettered_at": e.dead_lettered_at,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# POST /admin/webhooks/dlq/{id}/retry
# ---------------------------------------------------------------------------


@router.post("/webhooks/dlq/{entry_id}/retry", include_in_schema=False)
async def retry_webhook_dlq_entry(entry_id: int) -> dict:
    """Manually retry a single dead-lettered delivery."""
    entry = get_dlq_entry(entry_id, db_path=settings.db_path)
    if not entry:
        raise HTTPException(status_code=404, detail=f"DLQ entry {entry_id} not found")

    sub = get_subscriber(entry.subscriber_id, db_path=settings.db_path)
    if not sub:
        raise HTTPException(
            status_code=404,
            detail=f"Subscriber {entry.subscriber_id} not found; cannot re-sign payload",
        )

    queue = WebhookRetryQueue(db_path=settings.db_path)
    success = await queue.retry_dlq_entry(entry_id, secret=sub.secret)
    if not success:
        raise HTTPException(status_code=502, detail="Retry delivery failed; entry remains in DLQ")

    return {"id": entry_id, "status": "retried_and_delivered"}
