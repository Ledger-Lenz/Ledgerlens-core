"""Batch wallet scoring endpoint with async job queue (Issue #161)."""

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field

from config.settings import settings
from detection.storage import get_latest_scores, init_db

router = APIRouter(prefix="/scores", tags=["batch"])

# In-memory concurrency counter
_active_jobs: set[str] = set()
_MAX_CONCURRENT = 5
_JOB_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _init_batch_table() -> None:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


_init_batch_table()


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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_JOB_TTL_HOURS)).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM batch_jobs WHERE created_at < ?", (cutoff,))
        key_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=settings.batch_idempotency_window_hours)
        ).isoformat()
        conn.execute("DELETE FROM batch_idempotency WHERE created_at < ?", (key_cutoff,))


def _job_for_key(idempotency_key: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            """SELECT j.* FROM batch_idempotency k
               JOIN batch_jobs j ON j.job_id = k.job_id
               WHERE k.idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()


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

async def _score_wallet(wallet: str) -> dict:
    scores = await asyncio.get_event_loop().run_in_executor(
        None, lambda: get_latest_scores(wallet=wallet)
    )
    return {"wallet": wallet, "scores": [s.model_dump() for s in scores]}


async def _process_batch(job_id: str, wallets: list[str]) -> None:
    _update_job(job_id, status="processing")
    try:
        results = await asyncio.gather(*[_score_wallet(w) for w in wallets])
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
            result_json=json.dumps({"error": str(exc)}),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        _active_jobs.discard(job_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/batch", response_model=BatchJobQueued, status_code=202)
async def create_batch_job(
    body: BatchRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, max_length=255),
):
    """Queue a batch scoring job for up to 1000 wallets.

    Clients may send an ``Idempotency-Key`` header. A retry carrying the same
    key within ``batch_idempotency_window_hours`` returns the original job
    instead of queueing the batch again.
    """
    _expire_old_jobs()

    if idempotency_key:
        existing = _job_for_key(idempotency_key)
        if existing is not None:
            return BatchJobQueued(
                job_id=existing["job_id"],
                status=existing["status"],
                estimated_seconds=max(1, existing["total_wallets"] // 50),
            )

    if len(_active_jobs) >= _MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent batch jobs (max {_MAX_CONCURRENT}). Try again later.",
        )

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    estimated = max(1, len(body.wallets) // 50)

    # Reserve the concurrency slot synchronously so a burst of requests
    # arriving before any background task starts can't all pass the
    # `_active_jobs` check above.
    _active_jobs.add(job_id)

    with _connect() as conn:
        conn.execute(
            """INSERT INTO batch_jobs
               (job_id, status, priority, wallets_json, total_wallets, completed_wallets, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (job_id, "queued", body.priority, json.dumps(body.wallets), len(body.wallets), now),
        )
        if idempotency_key:
            try:
                conn.execute(
                    "INSERT INTO batch_idempotency (idempotency_key, job_id, created_at) VALUES (?, ?, ?)",
                    (idempotency_key, job_id, now),
                )
            except sqlite3.IntegrityError:
                # A concurrent request with the same key won the race; drop
                # our job and return theirs.
                conn.rollback()
                _active_jobs.discard(job_id)
                existing = _job_for_key(idempotency_key)
                if existing is None:
                    raise HTTPException(status_code=409, detail="Idempotency key conflict")
                return BatchJobQueued(
                    job_id=existing["job_id"],
                    status=existing["status"],
                    estimated_seconds=max(1, existing["total_wallets"] // 50),
                )

    background_tasks.add_task(_process_batch, job_id, body.wallets)
    return BatchJobQueued(job_id=job_id, status="queued", estimated_seconds=estimated)


@router.get("/batch/{job_id}", response_model=BatchJobStatus)
def get_batch_job(job_id: str):
    """Return the status and results of a batch scoring job."""
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
