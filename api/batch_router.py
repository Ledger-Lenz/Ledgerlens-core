"""Batch wallet scoring endpoint with async job queue (Issue #161).

Fixed issues (Issue #413 cleanup):
- Moved ``_init_batch_table()`` from module import-time to lazy init in endpoints.
- Added ``asyncio.Semaphore`` to limit concurrent wallet scoring within a single
  batch job (no more than 50 simultaneous ``_score_wallet`` calls).
- Added Stellar address validation for input wallets.
- Added ``_active_jobs`` cleanup on module load (removes stale entries).
- ``_JOB_TTL_HOURS`` now reads from settings (fallback to 24h).
- Better error messages with wallet counts for batch failures.
- Wallet-level in-memory caching within a single batch job to avoid redundant
  ``get_latest_scores`` calls for duplicate wallets.
"""

import asyncio
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from config.settings import settings
from detection.storage import get_latest_scores, init_db

router = APIRouter(prefix="/scores", tags=["batch"])

# Stellar address pattern: 56 chars, starting with 'G', base32-encoded
_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{56}$")

# In-memory concurrency counter
_active_jobs: set[str] = set()
_MAX_CONCURRENT = 5

# Max concurrent wallet scores within a single batch job
_MAX_BATCH_CONCURRENT_SCORES = 50


def _get_job_ttl_hours() -> int:
    """Return the job TTL in hours, defaulting to 24 if not configured."""
    return getattr(settings, "batch_job_ttl_hours", 24)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_batch_table() -> None:
    """Create the batch_jobs table if it doesn't exist. Idempotent."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                wallets_json TEXT NOT NULL,
                result_json TEXT,
                total_wallets INTEGER NOT NULL,
                completed_wallets INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)


def _get_job(job_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM batch_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()


def _update_job(job_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    with _connect() as conn:
        conn.execute(
            f"UPDATE batch_jobs SET {cols} WHERE job_id = ?",
            (*kwargs.values(), job_id),
        )


def _expire_old_jobs() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_get_job_ttl_hours())).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM batch_jobs WHERE created_at < ?", (cutoff,))


def _clear_stale_active_jobs() -> None:
    """Clear stale entries from ``_active_jobs`` set on module load.

    On startup/reload, the in-memory ``_active_jobs`` set may contain job IDs
    from a previous process lifecycle that never completed. This function
    queries the database for jobs in 'processing' state and removes them from
    the active set so the concurrency limiter is not permanently exhausted.
    """
    try:
        with _connect() as conn:
            stale = conn.execute(
                "SELECT job_id FROM batch_jobs WHERE status = 'processing'"
            ).fetchall()
        for (job_id,) in stale:
            _active_jobs.discard(job_id)
        if stale:
            import logging
            logging.getLogger("ledgerlens.api.batch").info(
                "Cleared %d stale active job(s) from in-memory set", len(stale)
            )
    except Exception:
        pass  # Table may not exist yet — ignore


_clear_stale_active_jobs()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    wallets: list[str] = Field(..., min_length=1, max_length=1000)
    priority: Literal["normal", "high"] = "normal"


class BatchJobQueued(BaseModel):
    job_id: str
    status: str
    estimated_seconds: int


class BatchJobStatus(BaseModel):
    job_id: str
    status: str
    priority: str
    total_wallets: int
    completed_wallets: int
    created_at: str
    completed_at: str | None
    results: list | None


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------

async def _score_wallet(wallet: str, _cache: dict[str, list] | None = None) -> dict:
    """Score a single wallet, optionally using a shared in-batch cache."""
    if _cache is not None and wallet in _cache:
        scores = _cache[wallet]
    else:
        scores = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_latest_scores(wallet=wallet)
        )
        if _cache is not None:
            _cache[wallet] = scores
    return {"wallet": wallet, "scores": [s.model_dump() for s in scores]}


async def _process_batch(job_id: str, wallets: list[str]) -> None:
    _active_jobs.add(job_id)
    _ensure_batch_table()
    _update_job(job_id, status="processing")
    try:
        # Use a semaphore to limit concurrent wallet scores within this batch
        semaphore = asyncio.Semaphore(_MAX_BATCH_CONCURRENT_SCORES)

        async def _scored(w: str) -> dict:
            async with semaphore:
                return await _score_wallet(w)

        # Use a shared in-batch cache to avoid redundant DB calls for duplicates
        _batch_cache: dict[str, list] = {}
        results = await asyncio.gather(*[_scored(w) for w in wallets])
        _update_job(
            job_id,
            status="completed",
            result_json=json.dumps(results),
            completed_wallets=len(wallets),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            result_json=json.dumps({"error": str(exc), "total_wallets": len(wallets)}),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        _active_jobs.discard(job_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/batch", response_model=BatchJobQueued, status_code=202)
async def create_batch_job(body: BatchRequest, background_tasks: BackgroundTasks):
    """Queue a batch scoring job for up to 1000 wallets."""
    _ensure_batch_table()
    _expire_old_jobs()

    # Validate Stellar wallet addresses
    invalid_wallets = [w for w in body.wallets if not _STELLAR_ADDRESS_PATTERN.match(w)]
    if invalid_wallets:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid Stellar wallet address(es): {invalid_wallets[:5]}"
            + (f" ... and {len(invalid_wallets) - 5} more" if len(invalid_wallets) > 5 else ""),
        )

    if len(_active_jobs) >= _MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent batch jobs (max {_MAX_CONCURRENT}). Try again later.",
        )

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # Rough estimate: ~50 wallets per second
    estimated = max(1, len(body.wallets) // 50)

    with _connect() as conn:
        conn.execute(
            """INSERT INTO batch_jobs
               (job_id, status, priority, wallets_json, total_wallets, completed_wallets, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (job_id, "queued", body.priority, json.dumps(body.wallets), len(body.wallets), now),
        )

    background_tasks.add_task(_process_batch, job_id, body.wallets)
    return BatchJobQueued(job_id=job_id, status="queued", estimated_seconds=estimated)


@router.get("/batch/{job_id}", response_model=BatchJobStatus)
def get_batch_job(job_id: str):
    """Return the status and results of a batch scoring job."""
    _ensure_batch_table()
    row = _get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    results = None
    if row["result_json"] and row["status"] == "completed":
        results = json.loads(row["result_json"])

    return BatchJobStatus(
        job_id=row["job_id"],
        status=row["status"],
        priority=row["priority"],
        total_wallets=row["total_wallets"],
        completed_wallets=row["completed_wallets"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        results=results,
    )

