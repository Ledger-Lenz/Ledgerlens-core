"""Durable, resumable queue for writes that must reach the Soroban registry.

Why this exists
---------------
A dispute resolution used to publish its zero-score override from a daemon
``threading.Thread`` spawned inside ``dispute_store.cast_vote``. That thread
marked the override ``'failed'`` before it even attempted the call and flipped
it to ``'submitted'`` only on confirmed success -- fail-safe as a default, but
nothing anywhere ever revisited a ``'failed'`` override. A network blip, a
rate limit, an expired key, or simply the process exiting before the daemon
thread finished left the wallet's local ``risk_scores`` row deleted while the
public on-chain registry that AMMs and lending protocols actually query kept
serving the old, disputed score indefinitely.

This module replaces that with a durable obligation: a row in
``pending_chain_submissions`` that survives process death, is retried with
exponential backoff, and can only ever produce one successful on-chain write
per originating decision.

Guarantees and how they are enforced
------------------------------------
* **Durable** -- the obligation is a committed database row, written in the
  same transaction as the decision that created it. There is no in-memory
  hand-off that a crash can drop.
* **Resumable** -- a worker claims a row by taking a time-boxed lease. If the
  process dies mid-flight the lease expires and the row becomes claimable
  again, so a restart picks up exactly where it left off.
* **Idempotent** -- ``idempotency_key`` is ``UNIQUE``. Enqueueing the same
  logical decision twice is a no-op, and the terminal ``'submitted'`` status
  is only ever reached once.
* **Single-flight** -- claiming is a conditional ``UPDATE`` whose ``WHERE``
  clause re-checks the state the reader saw. Two workers racing for the same
  row means one ``UPDATE`` matches and the other matches zero rows, so a row
  is never worked twice concurrently.
* **Priority-aware** -- rows carry a priority tier and are dequeued
  highest-priority-first, so a low-priority backfill backlog cannot starve a
  time-sensitive score update.
* **Observable** -- ``attempts``, ``last_error`` and ``status`` are columns,
  not log lines. :func:`queue_stats` exposes them for dashboards and alerts.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from config.settings import settings
from detection.risk_score import RiskScore
from detection.soroban_publisher import (
    SorobanCircuitOpenError,
    SorobanPublisher,
    SorobanSubmissionError,
)
from detection.storage import init_db

logger = logging.getLogger("ledgerlens.chain_submission_queue")

# Status values a row can hold. 'submitted', 'abandoned' and 'dead_letter'
# are terminal.
STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_SUBMITTED = "submitted"
STATUS_ABANDONED = "abandoned"
STATUS_DEAD_LETTER = "dead_letter"

KIND_DISPUTE_OVERRIDE = "dispute_override"

# Priority tiers. Higher numbers are dequeued first. ``PRIORITY_HIGH`` is for
# time-sensitive score updates; ``PRIORITY_LOW`` is for backfill work that may
# wait behind anything more urgent.
PRIORITY_LOW = 0
PRIORITY_NORMAL = 5
PRIORITY_HIGH = 10
DEFAULT_PRIORITY = PRIORITY_NORMAL

DEFAULT_MAX_ATTEMPTS = 10
# How long a worker may hold a claim before another worker may steal it. This
# must exceed the worst-case duration of a submission attempt, or a slow-but-
# alive worker will have its row taken from under it.
LEASE_SECONDS = 300
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600
# Fraction of the computed backoff applied as +/- jitter, so a fleet of
# workers that all failed against the same RPC outage does not retry in
# lockstep and re-trigger the outage.
BACKOFF_JITTER_FRACTION = 0.25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _backoff_seconds(attempts: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter, capped.

    ``attempts`` is the count *after* the failure that triggered this delay,
    so the first retry waits the base. Jitter is a uniform +/- fraction of the
    exponential delay, bounded so the result never goes negative and never
    exceeds the cap.
    """
    if attempts < 1:
        base = float(BACKOFF_BASE_SECONDS)
    else:
        base = float(min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_CAP_SECONDS))
    rng = rng or random
    jitter = base * BACKOFF_JITTER_FRACTION
    delay = base + rng.uniform(-jitter, jitter)
    return max(0.0, min(delay, float(BACKOFF_CAP_SECONDS)))


def _connect_rw(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection configured for multi-worker use.

    ``isolation_level=None`` puts the connection in autocommit mode so the
    explicit ``BEGIN IMMEDIATE`` in :func:`claim_next_due` actually takes the
    write lock at the point it is issued, rather than being deferred.
    """
    conn = sqlite3.connect(db_path or settings.db_path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def override_idempotency_key(dispute_id: str, override_id: int) -> str:
    """The stable identity of one dispute's on-chain override.

    Keyed on the dispute rather than the wallet: re-disputing the same wallet
    later is a genuinely new obligation, while retrying *this* dispute is not.
    """
    return f"{KIND_DISPUTE_OVERRIDE}:{dispute_id}:{override_id}"


def enqueue_override_submission(
    *,
    dispute_id: str,
    override_id: int,
    wallet: str,
    asset_pair: str,
    conn: sqlite3.Connection | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    priority: int = DEFAULT_PRIORITY,
) -> str:
    """Record a durable obligation to publish a zero-score override.

    Pass *conn* to enlist in the caller's open transaction -- ``cast_vote``
    does this so the obligation and the local state change it accompanies
    commit together or not at all.

    Returns the idempotency key. Enqueueing an already-queued decision is a
    no-op, so this is safe to call on a retried request.
    """
    key = override_idempotency_key(dispute_id, override_id)
    now = _now()
    payload = json.dumps({"score": 0, "reason": "dispute_override", "dispute_id": dispute_id})

    sql = """
        INSERT OR IGNORE INTO pending_chain_submissions (
            idempotency_key, kind, override_id, dispute_id, wallet, asset_pair,
            payload_json, status, attempts, max_attempts, priority,
            next_attempt_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
    """
    params = (
        key,
        KIND_DISPUTE_OVERRIDE,
        override_id,
        dispute_id,
        wallet,
        asset_pair,
        payload,
        STATUS_PENDING,
        max_attempts,
        priority,
        _iso(now),
        _iso(now),
        _iso(now),
    )

    if conn is not None:
        conn.execute(sql, params)
    else:
        owned = _connect_rw()
        try:
            owned.execute(sql, params)
        finally:
            owned.close()

    logger.info(
        "Queued on-chain override submission: key=%s wallet=%s pair=%s priority=%s",
        key,
        wallet,
        asset_pair,
        priority,
    )
    return key


def claim_next_due(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict | None:
    """Atomically claim the highest-priority due row, or ``None`` if none is due.

    Due means: still owed (``pending``, or ``in_flight`` with an expired lease
    left by a worker that died), and past its backoff. Rows are ordered by
    priority descending first, then by due time and id, so a high-priority
    submission is never starved by a low-priority backlog. The claim is a
    conditional ``UPDATE`` re-checking the status and lease the ``SELECT``
    saw, so two workers racing produce one winner and one no-op.
    """
    now = now or _now()
    now_iso = _iso(now)

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT id, idempotency_key, kind, override_id, dispute_id, wallet,
                   asset_pair, payload_json, status, attempts, max_attempts,
                   priority
            FROM pending_chain_submissions
            WHERE next_attempt_at <= ?
              AND (
                    status = ?
                 OR (status = ? AND (leased_until IS NULL OR leased_until <= ?))
              )
            ORDER BY priority DESC, next_attempt_at ASC, id ASC
            LIMIT 1
            """,
            (now_iso, STATUS_PENDING, STATUS_IN_FLIGHT, now_iso),
        ).fetchone()

        if row is None:
            conn.execute("COMMIT")
            return None

        lease_until = _iso(now + timedelta(seconds=LEASE_SECONDS))
        updated = conn.execute(
            """
            UPDATE pending_chain_submissions
               SET status = ?, leased_until = ?, updated_at = ?
             WHERE id = ?
               AND next_attempt_at <= ?
               AND (
                     status = ?
                  OR (status = ? AND (leased_until IS NULL OR leased_until <= ?))
               )
            """,
            (
                STATUS_IN_FLIGHT,
                lease_until,
                _iso(now),
                row[0],
                now_iso,
                STATUS_PENDING,
                STATUS_IN_FLIGHT,
                now_iso,
            ),
        ).rowcount

        if updated != 1:
            conn.execute("COMMIT")
            return None

        conn.execute("COMMIT")
        return {
            "id": row[0],
            "idempotency_key": row[1],
            "kind": row[2],
            "override_id": row[3],
            "dispute_id": row[4],
            "wallet": row[5],
            "asset_pair": row[6],
            "payload_json": row[7],
            "status": row[8],
            "attempts": row[9],
            "max_attempts": row[10],
            "priority": row[11],
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _dead_letter(
    conn: sqlite3.Connection,
    row: dict,
    *,
    error: str,
    now: datetime,
) -> None:
    """Route a submission that exhausted its retries to the dead-letter path.

    The row is moved to the terminal ``dead_letter`` status (never silently
    dropped) and an alert is emitted so operators can inspect and replay it.
    """
    conn.execute(
        """
        UPDATE pending_chain_submissions
           SET status = ?, last_error = ?, leased_until = NULL, updated_at = ?
         WHERE id = ?
        """,
        (STATUS_DEAD_LETTER, error, _iso(now), row["id"]),
    )
    logger.error(
        "DEAD-LETTER: submission key=%s wallet=%s pair=%s priority=%s "
        "exhausted %s attempts; last_error=%s",
        row.get("idempotency_key"),
        row.get("wallet"),
        row.get("asset_pair"),
        row.get("priority"),
        row.get("max_attempts"),
        error,
    )


def record_failure(
    conn: sqlite3.Connection,
    row: dict,
    *,
    error: str,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> str:
    """Record a failed attempt, scheduling a jittered backoff retry.

    Returns the resulting status: ``STATUS_DEAD_LETTER`` when the row has
    exhausted ``max_attempts``, otherwise ``STATUS_PENDING`` with
    ``next_attempt_at`` pushed out by exponential backoff plus jitter.
    """
    now = now or _now()
    attempts = int(row.get("attempts", 0)) + 1
    max_attempts = int(row.get("max_attempts", DEFAULT_MAX_ATTEMPTS))

    if attempts >= max_attempts:
        _dead_letter(conn, row, error=error, now=now)
        return STATUS_DEAD_LETTER

    delay = _backoff_seconds(attempts, rng=rng)
    next_attempt = now + timedelta(seconds=delay)
    conn.execute(
        """
        UPDATE pending_chain_submissions
           SET status = ?, attempts = ?, last_error = ?, leased_until = NULL,
               next_attempt_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            STATUS_PENDING,
            attempts,
            error,
            _iso(next_attempt),
            _iso(now),
            row["id"],
        ),
    )
    logger.warning(
        "Submission key=%s failed (attempt %s/%s); retrying in %.1fs: %s",
        row.get("idempotency_key"),
        attempts,
        max_attempts,
        delay,
        error,
    )
    return STATUS_PENDING


def dead_lettered(conn: sqlite3.Connection) -> list[dict]:
    """Return all dead-lettered submissions for inspection or replay."""
    rows = conn.execute(
        """
        SELECT id, idempotency_key, kind, override_id, dispute_id, wallet,
               asset_pair, payload_json, attempts, max_attempts, priority,
               last_error, updated_at
        FROM pending_chain_submissions
        WHERE status = ?
        ORDER BY updated_at ASC, id ASC
        """,
        (STATUS_DEAD_LETTER,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "idempotency_key": r[1],
            "kind": r[2],
            "override_id": r[3],
            "dispute_id": r[4],
            "wallet": r[5],
            "asset_pair": r[6],
            "payload_json": r[7],
            "attempts": r[8],
            "max_attempts": r[9],
            "priority": r[10],
            "last_error": r[11],
            "updated_at": r[12],
        }
        for r in rows
    ]
