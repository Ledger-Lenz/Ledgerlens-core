"""Admin REST API for model lifecycle and system configuration (Issue #160)."""

import glob
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings, _runtime_cache
from detection.model_registry import get_current_version, list_model_versions

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
# PSI heatmap & history endpoints
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SHAP feature importance endpoints
# ---------------------------------------------------------------------------


@router.get("/feature-importance/{version}", include_in_schema=False)
def feature_importance(version: str, model_name: str | None = None) -> dict:
    """Return stored SHAP importance data for a given model version."""
    from detection.model_registry import load_shap_importances

    importances = load_shap_importances(settings.model_dir)
    if importances is None:
        raise HTTPException(status_code=404, detail=f"No SHAP importance data found")

    if model_name:
        filtered = {model_name: importances.get(model_name, [])}
        return {"version": version, "shap_importances": filtered}

    return {"version": version, "shap_importances": importances}


@router.get("/feature-importance/diff", include_in_schema=False)
def feature_importance_diff(old: str = "old", new: str = "new") -> dict:
    """Compare SHAP importance rankings between two model versions."""
    import json as _json
    from detection.model_registry import compare_importance_stability

    metadata_path = os.path.join(settings.model_dir, "training_metadata.json")
    if not os.path.exists(metadata_path):
        raise HTTPException(status_code=404, detail="Training metadata not found")

    with open(metadata_path, "r") as f:
        metadata = _json.load(f)

    old_meta = {"version": old, "shap_importances": metadata.get("shap_importances", {})}
    new_meta = {"version": new, "shap_importances": metadata.get("shap_importances", {})}

    report = compare_importance_stability(old_meta, new_meta)
    return {
        "version_old": report.version_old,
        "version_new": report.version_new,
        "spearman_rho": report.spearman_rho,
        "stable": report.stable,
        "changed_features": report.changed_features,
        "computed_at": report.computed_at.isoformat(),
    }


@router.get("/psi-heatmap", include_in_schema=False)
def psi_heatmap(days: int = 90):
    """Return the most recently generated PSI heatmap as a PNG image."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from detection.drift_monitor import export_psi_heatmap

    heatmap_dir = Path(settings.model_dir) / "psi_heatmaps"
    heatmap_dir.mkdir(parents=True, exist_ok=True)
    heatmap_path = heatmap_dir / "latest_heatmap.png"

    export_psi_heatmap(heatmap_path, days_back=days)
    return FileResponse(str(heatmap_path), media_type="image/png")


@router.get("/psi-history", include_in_schema=False)
def psi_history(
    feature: str | None = None,
    days: int = 30,
) -> list[dict]:
    """Return per-feature PSI history records."""
    from detection.drift_monitor import load_psi_history

    df = load_psi_history(days_back=days)
    if df.empty:
        return []

    if feature:
        df = df[df["feature_name"] == feature]

    return [
        {
            "feature_name": row["feature_name"],
            "psi_value": row["psi_value"],
            "computed_at": row["computed_at"],
        }
        for _, row in df.iterrows()
    ]
