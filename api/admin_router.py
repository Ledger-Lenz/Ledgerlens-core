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
from api.webhook_sender import list_dlq, get_dlq_entry
from api.webhook_sender import WebhookRetryQueue
from detection.webhook_registry import get_subscriber
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
# GET /admin/oracle/status
# ---------------------------------------------------------------------------


@router.get("/oracle/status", include_in_schema=False)
def oracle_status() -> list[dict]:
    """Return the status of the oracle nodes in the quorum."""
    from detection.oracle_node import OracleNode
    from config.settings import settings as _settings
    nodes = [OracleNode(name, key) for name, key in getattr(_settings, "oracle_nodes", {}).items()]
    return [
        {
            "name": node.name,
            "public_key": node.public_key_hex,
            "last_seen": getattr(node, "last_seen", None),
        }
        for node in nodes
    ]


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


# ---------------------------------------------------------------------------
# GET /admin/shadow/report
# ---------------------------------------------------------------------------


@router.get("/shadow/report", include_in_schema=False)
def shadow_report() -> dict:
    """Return shadow model scoring report: mean divergence, p95, high-divergence wallets."""
    from detection.shadow_scoring import get_shadow_model_version, get_shadow_report

    version = get_shadow_model_version()
    if not version:
        raise HTTPException(status_code=404, detail="Shadow mode not active (SHADOW_MODEL_VERSION not set)")

    report = get_shadow_report(settings.db_path)
    report["shadow_model_version"] = version
    return report


# ---------------------------------------------------------------------------
# POST /admin/retrain
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# GET /admin/feature-store/stats
# ---------------------------------------------------------------------------


class FeatureStoreStats(BaseModel):
    hot_tier_rows: int
    cold_tier_rows: int
    oldest_hot_record: Optional[datetime]
    oldest_cold_record: Optional[datetime]
    archive_dir_size_mb: float


@router.get("/feature-store/stats", response_model=FeatureStoreStats, include_in_schema=False)
def feature_store_stats() -> FeatureStoreStats:
    """Return hot-tier row count, cold-tier row count, oldest timestamps, and archive size."""
    from pathlib import Path

    from detection.feature_store import ParquetFeatureColdTier

    # Hot tier: count rows and find oldest record in SQLite
    hot_rows = 0
    oldest_hot: Optional[datetime] = None
    try:
        with sqlite3.connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(recorded_at) FROM feature_distribution_snapshots"
            ).fetchone()
            if row:
                hot_rows = row[0] or 0
                if row[1]:
                    oldest_hot = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
    except Exception:
        pass

    # Cold tier: count rows and find oldest record in Parquet
    archive_dir = Path(settings.feature_archive_dir)
    cold = ParquetFeatureColdTier(archive_dir)
    cold_rows = cold.row_count()
    oldest_cold = cold.oldest_record()

    # Archive directory size on disk
    archive_size_mb = 0.0
    try:
        if archive_dir.exists():
            total_bytes = sum(
                f.stat().st_size for f in archive_dir.rglob("*") if f.is_file()
            )
            archive_size_mb = round(total_bytes / (1024 * 1024), 3)
    except Exception:
        pass

    return FeatureStoreStats(
        hot_tier_rows=hot_rows,
        cold_tier_rows=cold_rows,
        oldest_hot_record=oldest_hot,
        oldest_cold_record=oldest_cold,
        archive_dir_size_mb=archive_size_mb,
    )


class FLPrivacyStatus(BaseModel):
    current_epsilon: float
    target_epsilon: float
    delta: float
    noise_multiplier: float
    clip_norm: float
    budget_exhausted: bool
    rounds_completed: int


@router.get("/storage", include_in_schema=False)
def storage_stats() -> dict:
    """Return current database size, per-table row counts, and next archival date."""
    from storage.retention import RetentionEngine
    engine = RetentionEngine(db_path=settings.db_path)
    return engine.storage_stats()


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


# ---------------------------------------------------------------------------
# Alert suppression rules  (Issue #178)
# ---------------------------------------------------------------------------


class SuppressionCreate(BaseModel):
    wallet: str
    reason: str
    expires_at: Optional[str] = None


@router.post("/suppressions", status_code=201, include_in_schema=False)
def add_suppression(body: SuppressionCreate) -> dict:
    """Add an alert suppression rule for a wallet.

    The wallet will generate no alert events while an active rule exists.
    Rules expire automatically at ``expires_at`` (ISO-8601 UTC); omit to
    create a permanent rule.
    """
    from detection.suppressions import get_store
    store = get_store()
    return store.add(wallet=body.wallet, reason=body.reason, expires_at=body.expires_at)


@router.get("/suppressions", include_in_schema=False)
def list_suppressions() -> list[dict]:
    """Return all currently active (non-expired) suppression rules."""
    from detection.suppressions import get_store
    return get_store().list_active()


@router.delete("/suppressions/{rule_id}", include_in_schema=False)
def delete_suppression(rule_id: int) -> dict:
    """Remove a suppression rule by ID."""
    from detection.suppressions import get_store
    deleted = get_store().delete(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Suppression rule {rule_id} not found")
    return {"deleted": rule_id}


# ---------------------------------------------------------------------------
# Database storage stats  (Issue #180)
# ---------------------------------------------------------------------------


@router.get("/storage", include_in_schema=False)
def storage_stats() -> dict:
    """Return current database size, per-table row counts, and next archival date."""
    from storage.retention import RetentionEngine
    engine = RetentionEngine(db_path=settings.db_path)
    return engine.storage_stats()
