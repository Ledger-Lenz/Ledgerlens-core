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
* **Observable** -- ``attempts``, ``last_error`` and ``status`` are columns,
  not log lines. :func:`queue_stats` exposes them for dashboards and alerts.
"""

from __future__ import annotations

import json
import logging
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

# Status values a row can hold. 'submitted' and 'abandoned' are terminal.
STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_SUBMITTED = "submitted"
STATUS_ABANDONED = "abandoned"

KIND_DISPUTE_OVERRIDE = "dispute_override"

DEFAULT_MAX_ATTEMPTS = 10
# How long a worker may hold a claim before another worker may steal it. This
# must exceed the worst-case duration of a submission attempt, or a slow-but-
# alive worker will have its row taken from under it.
LEASE_SECONDS = 300
BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff, capped. ``attempts`` is the count *after* the
    failure that triggered this delay, so the first retry waits the base."""
    if attempts < 1:
        return BACKOFF_BASE_SECONDS
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
    return int(min(delay, BACKOFF_CAP_SECONDS))


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
            payload_json, status, attempts, max_attempts, next_attempt_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
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
        "Queued on-chain override submission: key=%s wallet=%s pair=%s",
        key,
        wallet,
        asset_pair,
    )
    return key


def claim_next_due(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict | None:
    """Atomically claim the oldest due row, or return ``None`` if none is due.

    Due means: still owed (``pending``, or ``in_flight`` with an expired lease
    left by a worker that died), and past its backoff. The claim is a
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
                   asset_pair, payload_json, status, attempts, max_attempts
            FROM pending_chain_submissions
            WHERE next_attempt_at <= ?
              AND (
                    status = ?
                 OR (status = ? AND (leased_until IS NULL OR leased_until <= ?))
              )
            ORDER BY next_attempt_at ASC, id ASC
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
                now_iso,
                row[0],
                now_iso,
                STATUS_PENDING,
                STATUS_IN_FLIGHT,
                now_iso,
            ),
        ).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if updated != 1:
        # Another worker won the race for this row.
        return None

    return {
        "id": row[0],
        "idempotency_key": row[1],
        "kind": row[2],
        "override_id": row[3],
        "dispute_id": row[4],
        "wallet": row[5],
        "asset_pair": row[6],
        "payload": json.loads(row[7]),
        "attempts": row[9],
        "max_attempts": row[10],
    }


def _mark_submitted(conn: sqlite3.Connection, job_id: int, tx_hash: str | None) -> None:
    now_iso = _iso(_now())
    conn.execute(
        """
        UPDATE pending_chain_submissions
           SET status = ?, tx_hash = ?, last_error = NULL, leased_until = NULL,
               attempts = attempts + 1, updated_at = ?
         WHERE id = ? AND status != ?
        """,
        (STATUS_SUBMITTED, tx_hash, now_iso, job_id, STATUS_SUBMITTED),
    )


def _mark_retry(conn: sqlite3.Connection, job: dict, error: str) -> str:
    """Schedule a retry, or give up once ``max_attempts`` is exhausted.

    Returns the status the row was moved to.
    """
    attempts = job["attempts"] + 1
    now = _now()
    if attempts >= job["max_attempts"]:
        conn.execute(
            """
            UPDATE pending_chain_submissions
               SET status = ?, attempts = ?, last_error = ?, leased_until = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (STATUS_ABANDONED, attempts, error, _iso(now), job["id"]),
        )
        logger.error(
            "On-chain submission abandoned after %d attempts: key=%s last_error=%s",
            attempts,
            job["idempotency_key"],
            error,
        )
        return STATUS_ABANDONED

    next_at = now + timedelta(seconds=_backoff_seconds(attempts))
    conn.execute(
        """
        UPDATE pending_chain_submissions
           SET status = ?, attempts = ?, last_error = ?, leased_until = NULL,
               next_attempt_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (STATUS_PENDING, attempts, error, _iso(next_at), _iso(now), job["id"]),
    )
    logger.warning(
        "On-chain submission attempt %d failed, retrying at %s: key=%s error=%s",
        attempts,
        _iso(next_at),
        job["idempotency_key"],
        error,
    )
    return STATUS_PENDING


def _build_publisher() -> SorobanPublisher:
    return SorobanPublisher(
        contract_id=settings.score_contract_id,
        secret_key=settings.service_secret_key,
        soroban_rpc_url=settings.soroban_rpc_url,
        network_passphrase=settings.network_passphrase,
        circuit_breaker_threshold=settings.soroban_circuit_breaker_threshold,
        circuit_reset_seconds=settings.soroban_circuit_reset_seconds,
    )


def _publish(job: dict, publisher: SorobanPublisher) -> str | None:
    if job["kind"] != KIND_DISPUTE_OVERRIDE:
        raise SorobanSubmissionError(f"Unknown submission kind: {job['kind']}")

    zero_score = RiskScore(
        wallet=job["wallet"],
        asset_pair=job["asset_pair"],
        score=0,
        benford_flag=False,
        ml_flag=False,
        confidence=0,
        timestamp=_now(),
    )
    return publisher.submit_score(zero_score)


def process_once(
    *,
    conn: sqlite3.Connection | None = None,
    publisher: SorobanPublisher | None = None,
    on_submitted=None,
) -> dict | None:
    """Claim and work at most one due submission.

    *on_submitted* is called as ``on_submitted(job, tx_hash)`` after a
    confirmed write, which is how ``score_overrides.status`` is kept in step
    without this module having to know that table's shape.

    Returns the job dict with a ``result`` key, or ``None`` if nothing was due.
    """
    owned_conn = conn is None
    conn = conn or _connect_rw()
    try:
        job = claim_next_due(conn)
        if job is None:
            return None

        publisher = publisher or _build_publisher()
        try:
            tx_hash = _publish(job, publisher)
        except (SorobanCircuitOpenError, SorobanSubmissionError) as exc:
            job["result"] = _mark_retry(conn, job, f"{type(exc).__name__}: {exc}")
            return job
        except Exception as exc:  # pragma: no cover - defensive
            job["result"] = _mark_retry(conn, job, f"{type(exc).__name__}: {exc}")
            return job

        if not tx_hash:
            # A dry-run or skipped submission is not a confirmed write, so the
            # obligation stays open rather than being quietly closed.
            job["result"] = _mark_retry(conn, job, "publisher returned no transaction hash")
            return job

        _mark_submitted(conn, job["id"], tx_hash)
        job["result"] = STATUS_SUBMITTED
        job["tx_hash"] = tx_hash
        logger.info(
            "On-chain submission confirmed: key=%s tx_hash=%s",
            job["idempotency_key"],
            tx_hash,
        )
        if on_submitted is not None:
            on_submitted(job, tx_hash)
        return job
    finally:
        if owned_conn:
            conn.close()


def run_worker(
    *,
    poll_interval: float = 5.0,
    max_iterations: int | None = None,
    publisher: SorobanPublisher | None = None,
    on_submitted=None,
) -> int:
    """Drain the queue, sleeping between polls. Returns jobs worked.

    *max_iterations* bounds the loop so callers (and tests) can run it to
    completion instead of forever.
    """
    init_db()
    worked = 0
    iterations = 0
    conn = _connect_rw()
    try:
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            job = process_once(conn=conn, publisher=publisher, on_submitted=on_submitted)
            if job is None:
                if max_iterations is not None:
                    break
                time.sleep(poll_interval)
                continue
            worked += 1
    finally:
        conn.close()
    return worked


def queue_stats(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Queue depth by status, for dashboards and alerting."""
    owned = conn is None
    conn = conn or _connect_rw()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM pending_chain_submissions GROUP BY status"
        ).fetchall()
        stats = {status: count for status, count in rows}
        stats["total"] = sum(count for _, count in rows)
        return stats
    finally:
        if owned:
            conn.close()
