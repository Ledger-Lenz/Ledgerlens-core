"""Analyst review store — persistence for analyst feedback and case management.

Stores analyst verdicts on flagged wallets, manages case assignments with
soft locking, and provides the query layer for the analyst review dashboard
API (Issue #200).  Records are consumed by the active learning loop via
GET /analyst/feedback?since=<ISO_TIMESTAMP>.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Literal

from detection.storage import _connect, init_db
from config.settings import settings

VerdictType = Literal["confirmed_wash", "false_positive", "needs_review"]

_VALID_VERDICTS: frozenset[str] = frozenset(
    ["confirmed_wash", "false_positive", "needs_review"]
)


# ---------------------------------------------------------------------------
# Case assignment — claim / release / expire
# ---------------------------------------------------------------------------


def claim_wallet(
    wallet: str,
    asset_pair: str,
    analyst_key_hash: str,
    lock_timeout_seconds: int | None = None,
    db_path: str | None = None,
) -> dict:
    """Atomically claim a wallet for review.

    Uses a single INSERT … ON CONFLICT statement to avoid read-then-write races.
    A claim succeeds when no active (non-expired, non-released, non-resolved)
    assignment exists for this wallet+asset_pair.  If an existing active claim
    is expired, the new claim replaces it.

    Returns:
        Claim record dict on success.

    Raises:
        PermissionError: If the analyst already holds the maximum concurrent claims.
        RuntimeError: If another analyst holds an active (non-expired) claim.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)
    timeout = lock_timeout_seconds if lock_timeout_seconds is not None else settings.analyst_lock_timeout_seconds
    lock_expires = now + timedelta(seconds=timeout)

    with _connect(db_path) as conn:
        # Use IMMEDIATE transaction to serialize concurrent claim attempts
        conn.execute("BEGIN IMMEDIATE")

        # Enforce per-analyst claim cap (only count active, non-expired claims)
        active_count = conn.execute(
            """
            SELECT COUNT(*) FROM case_assignments
            WHERE analyst_key_hash = ? AND status = 'assigned'
              AND lock_expires_at > ?
            """,
            (analyst_key_hash, now.isoformat()),
        ).fetchone()[0]

        if active_count >= settings.analyst_claim_max_active_per_analyst:
            conn.execute("ROLLBACK")
            raise PermissionError(
                f"Analyst already holds {active_count} active claims "
                f"(max {settings.analyst_claim_max_active_per_analyst})"
            )

        # Check if there's a current active (non-expired) claim by someone else
        existing = conn.execute(
            """
            SELECT analyst_key_hash, lock_expires_at
            FROM case_assignments
            WHERE wallet = ? AND asset_pair = ? AND status = 'assigned'
              AND lock_expires_at > ?
            """,
            (wallet, asset_pair, now.isoformat()),
        ).fetchone()

        if existing and existing[0] != analyst_key_hash:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"Already claimed by {existing[0]} until {existing[1]}"
            )

        # If this analyst already holds the claim, refresh the lock
        if existing and existing[0] == analyst_key_hash:
            conn.execute(
                """
                UPDATE case_assignments
                SET lock_expires_at = ?, assigned_at = ?
                WHERE wallet = ? AND asset_pair = ? AND status = 'assigned'
                  AND analyst_key_hash = ? AND lock_expires_at > ?
                """,
                (lock_expires.isoformat(), now.isoformat(),
                 wallet, asset_pair, analyst_key_hash, now.isoformat()),
            )
            conn.commit()
            return {
                "wallet": wallet,
                "asset_pair": asset_pair,
                "analyst_key_hash": analyst_key_hash,
                "assigned_at": now.isoformat(),
                "lock_expires_at": lock_expires.isoformat(),
            }

        # Either no active claim exists, or it's expired — insert a new one
        # Expire any stale rows first
        conn.execute(
            """
            UPDATE case_assignments
            SET status = 'released', released_at = ?
            WHERE wallet = ? AND asset_pair = ? AND status = 'assigned'
              AND lock_expires_at <= ?
            """,
            (now.isoformat(), wallet, asset_pair, now.isoformat()),
        )

        conn.execute(
            """
            INSERT INTO case_assignments
                (wallet, asset_pair, analyst_key_hash, assigned_at, lock_expires_at, status)
            VALUES (?, ?, ?, ?, ?, 'assigned')
            """,
            (wallet, asset_pair, analyst_key_hash, now.isoformat(), lock_expires.isoformat()),
        )
        conn.commit()

    return {
        "wallet": wallet,
        "asset_pair": asset_pair,
        "analyst_key_hash": analyst_key_hash,
        "assigned_at": now.isoformat(),
        "lock_expires_at": lock_expires.isoformat(),
    }


def release_wallet(
    wallet: str,
    asset_pair: str,
    analyst_key_hash: str,
    db_path: str | None = None,
) -> bool:
    """Explicitly release an analyst's claim on a wallet.

    Returns True if a claim was released, False if no active claim existed.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE case_assignments
            SET status = 'released', released_at = ?
            WHERE wallet = ? AND asset_pair = ? AND analyst_key_hash = ?
              AND status = 'assigned' AND lock_expires_at > ?
            """,
            (now.isoformat(), wallet, asset_pair, analyst_key_hash, now.isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def resolve_claim(
    wallet: str,
    asset_pair: str,
    analyst_key_hash: str,
    db_path: str | None = None,
) -> bool:
    """Mark an analyst's claim as resolved (verdict submitted).

    Returns True if a claim was resolved, False if no active claim existed.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE case_assignments
            SET status = 'resolved', resolved_at = ?
            WHERE wallet = ? AND asset_pair = ? AND analyst_key_hash = ?
              AND status = 'assigned' AND lock_expires_at > ?
            """,
            (now.isoformat(), wallet, asset_pair, analyst_key_hash, now.isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def expire_stale_locks(db_path: str | None = None) -> int:
    """Release all claims whose lock has expired.

    Returns the number of locks released.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE case_assignments
            SET status = 'released', released_at = ?
            WHERE status = 'assigned' AND lock_expires_at <= ?
            """,
            (now.isoformat(), now.isoformat()),
        )
        conn.commit()
        return cur.rowcount


def get_active_claim(
    wallet: str,
    asset_pair: str,
    db_path: str | None = None,
) -> dict | None:
    """Return the active (non-expired) claim for a wallet, or None."""
    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT analyst_key_hash, assigned_at, lock_expires_at
            FROM case_assignments
            WHERE wallet = ? AND asset_pair = ? AND status = 'assigned'
              AND lock_expires_at > ?
            ORDER BY assigned_at DESC
            LIMIT 1
            """,
            (wallet, asset_pair, now.isoformat()),
        ).fetchone()

    if row is None:
        return None
    return {
        "analyst_key_hash": row[0],
        "assigned_at": row[1],
        "lock_expires_at": row[2],
    }


# ---------------------------------------------------------------------------
# Write path — feedback submission (requires active claim)
# ---------------------------------------------------------------------------


def submit_analyst_feedback(
    wallet: str,
    asset_pair: str,
    verdict: str,
    notes: str | None,
    analyst_key_hash: str,
    review_started_at: datetime | None = None,
    require_claim: bool = True,
    db_path: str | None = None,
) -> dict:
    """Record an analyst verdict for ``wallet`` / ``asset_pair``.

    Args:
        wallet: Stellar wallet address.
        asset_pair: Asset pair (e.g. ``XLM/USDC``).
        verdict: One of ``confirmed_wash``, ``false_positive``, ``needs_review``.
        notes: Optional free-text analyst notes.
        analyst_key_hash: SHA-256 hex hash of the analyst's identity key.
        review_started_at: When the analyst started reviewing (for avg-review-time stats).
        require_claim: When True, an active claim by the same analyst is required.
        db_path: Override DB path (defaults to settings.db_path).

    Returns:
        The persisted feedback record as a dict.

    Raises:
        ValueError: If verdict is not one of the accepted values.
        PermissionError: If require_claim is True and no active claim exists.
        RuntimeError: If require_claim is True and the claim is held by another analyst.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"Invalid verdict '{verdict}'. Must be one of {sorted(_VALID_VERDICTS)}"
        )

    if require_claim:
        claim = get_active_claim(wallet, asset_pair, db_path=db_path)
        if claim is None:
            raise PermissionError(
                "No active claim on this wallet. Claim it first via POST /analyst/wallet/{wallet}/claim"
            )
        if claim["analyst_key_hash"] != analyst_key_hash:
            raise RuntimeError(
                f"Wallet is claimed by another analyst (hash {claim['analyst_key_hash'][:8]}…)"
            )

    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO analyst_feedback
                (wallet, asset_pair, verdict, notes, analyst_key_hash, submitted_at, review_started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                asset_pair,
                verdict,
                notes,
                analyst_key_hash,
                now.isoformat(),
                review_started_at.isoformat() if review_started_at else None,
            ),
        )
        conn.commit()
        record_id = cur.lastrowid

    # Mark the claim as resolved after successful feedback submission
    if require_claim:
        resolve_claim(wallet, asset_pair, analyst_key_hash, db_path=db_path)

    return {
        "id": record_id,
        "wallet": wallet,
        "asset_pair": asset_pair,
        "verdict": verdict,
        "notes": notes,
        "analyst_key_hash": analyst_key_hash,
        "submitted_at": now.isoformat(),
        "review_started_at": review_started_at.isoformat() if review_started_at else None,
    }


# ---------------------------------------------------------------------------
# Read path — queue (annotated with assignment state)
# ---------------------------------------------------------------------------


def get_analyst_queue(limit: int = 20, db_path: str | None = None) -> list[dict]:
    """Return top ``limit`` wallets awaiting analyst review, sorted by score descending.

    A wallet is "awaiting review" when it has a risk score >= threshold and has
    no analyst feedback submitted today.

    Each item is annotated with its current assignment state:
    - ``assigned``: True/False whether the wallet has an active claim
    - ``assigned_to``: analyst_key_hash of current claimant (None if unassigned)
    - ``lock_expires_at``: ISO timestamp of lock expiry (None if unassigned)
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)

    today_start = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                rs.wallet,
                rs.asset_pair,
                rs.score,
                rs.timestamp,
                EXISTS (
                    SELECT 1 FROM analyst_feedback af
                    WHERE af.wallet = rs.wallet
                      AND af.asset_pair = rs.asset_pair
                      AND af.submitted_at >= ?
                ) AS reviewed_today,
                ca.analyst_key_hash,
                ca.lock_expires_at,
                CASE
                    WHEN ca.analyst_key_hash IS NOT NULL
                         AND ca.status = 'assigned'
                         AND ca.lock_expires_at > ?
                    THEN 1 ELSE 0
                END AS is_assigned
            FROM risk_scores rs
            INNER JOIN (
                SELECT wallet, asset_pair, MAX(id) AS max_id
                FROM risk_scores
                GROUP BY wallet, asset_pair
            ) latest ON rs.id = latest.max_id
            LEFT JOIN case_assignments ca
                ON ca.wallet = rs.wallet
                AND ca.asset_pair = rs.asset_pair
                AND ca.status = 'assigned'
                AND ca.lock_expires_at > ?
            WHERE reviewed_today = 0
            ORDER BY rs.score DESC
            LIMIT ?
            """,
            (today_start, now.isoformat(), now.isoformat(), limit),
        ).fetchall()

    return [
        {
            "wallet": row[0],
            "asset_pair": row[1],
            "score": row[2],
            "last_scored_at": row[3],
            "reviewed_today": bool(row[4]),
            "is_assigned": bool(row[7]),
            "assigned_to": row[5] if row[7] else None,
            "lock_expires_at": row[6] if row[7] else None,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Read path — feedback export
# ---------------------------------------------------------------------------


def get_analyst_feedback_since(
    since: datetime,
    db_path: str | None = None,
) -> list[dict]:
    """Return all feedback records submitted at or after ``since``.

    Used by the active learning loop to consume new labels.
    """
    init_db(db_path)

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, wallet, asset_pair, verdict, notes, analyst_key_hash,
                   submitted_at, review_started_at
            FROM analyst_feedback
            WHERE submitted_at >= ?
            ORDER BY submitted_at ASC
            """,
            (since.isoformat(),),
        ).fetchall()

    return [
        {
            "id": row[0],
            "wallet": row[1],
            "asset_pair": row[2],
            "verdict": row[3],
            "notes": row[4],
            "analyst_key_hash": row[5],
            "submitted_at": row[6],
            "review_started_at": row[7],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Read path — aggregate stats
# ---------------------------------------------------------------------------


def get_analyst_stats(db_path: str | None = None) -> dict:
    """Return aggregate analyst review statistics.

    Returns:
        dict with keys:
        - cases_reviewed_today: int
        - false_positive_rate_30d: float (0.0–1.0)
        - avg_review_time_seconds: float | None
    """
    init_db(db_path)

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with _connect(db_path) as conn:
        # Cases reviewed today
        today_count = conn.execute(
            "SELECT COUNT(*) FROM analyst_feedback WHERE submitted_at >= ?",
            (today_start,),
        ).fetchone()[0]

        # False positive rate over last 30 days
        fp_rows = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN verdict = 'false_positive' THEN 1 ELSE 0 END) AS fp_count
            FROM analyst_feedback
            WHERE submitted_at >= ?
              AND verdict IN ('confirmed_wash', 'false_positive')
            """,
            (thirty_days_ago,),
        ).fetchone()
        total_30d = fp_rows[0] or 0
        fp_count_30d = fp_rows[1] or 0
        fp_rate = (fp_count_30d / total_30d) if total_30d > 0 else 0.0

        # Average review time (seconds) where review_started_at is known
        avg_row = conn.execute(
            """
            SELECT AVG(
                CAST(
                    (julianday(submitted_at) - julianday(review_started_at)) * 86400
                    AS REAL
                )
            )
            FROM analyst_feedback
            WHERE review_started_at IS NOT NULL
              AND submitted_at >= ?
            """,
            (thirty_days_ago,),
        ).fetchone()
        avg_review_time = avg_row[0]

    return {
        "cases_reviewed_today": today_count,
        "false_positive_rate_30d": round(fp_rate, 4),
        "avg_review_time_seconds": round(avg_review_time, 1) if avg_review_time is not None else None,
    }


# ---------------------------------------------------------------------------
# Read path — case stats (SLA metrics)
# ---------------------------------------------------------------------------


def get_case_stats(db_path: str | None = None) -> dict:
    """Return SLA and case-management metrics.

    Returns:
        dict with keys:
        - avg_time_to_claim_seconds: float | None — average seconds between
          queue appearance and first claim
        - avg_time_to_resolution_seconds: float | None — average seconds
          between claim and resolution (verdict submitted)
        - assigned_count: int — wallets currently assigned (active claims)
        - unassigned_count: int — wallets in the queue with no active claim
        - expired_reclaimed_count: int — locks released due to expiry
    """
    init_db(db_path)
    now = datetime.now(timezone.utc)

    with _connect(db_path) as conn:
        # Average time-to-claim: we approximate this as the time between
        # the risk_score timestamp and the assignment's assigned_at
        avg_ttc_row = conn.execute(
            """
            SELECT AVG(
                CAST(
                    (julianday(ca.assigned_at) - julianday(rs.timestamp)) * 86400
                    AS REAL
                )
            )
            FROM case_assignments ca
            INNER JOIN risk_scores rs
                ON rs.wallet = ca.wallet AND rs.asset_pair = ca.asset_pair
            WHERE ca.assigned_at IS NOT NULL
            """,
        ).fetchone()
        avg_ttc = avg_ttc_row[0]

        # Average time-to-resolution: assigned_at → resolved_at
        avg_ttr_row = conn.execute(
            """
            SELECT AVG(
                CAST(
                    (julianday(resolved_at) - julianday(assigned_at)) * 86400
                    AS REAL
                )
            )
            FROM case_assignments
            WHERE resolved_at IS NOT NULL
              AND assigned_at IS NOT NULL
            """,
        ).fetchone()
        avg_ttr = avg_ttr_row[0]

        # Currently assigned (active, non-expired claims)
        assigned_count = conn.execute(
            """
            SELECT COUNT(*) FROM case_assignments
            WHERE status = 'assigned' AND lock_expires_at > ?
            """,
            (now.isoformat(),),
        ).fetchone()[0]

        # Unassigned in queue: wallets in risk_scores that are in the queue
        # (no today feedback) AND have no active claim
        today_start = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        unassigned_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM risk_scores rs
            INNER JOIN (
                SELECT wallet, asset_pair, MAX(id) AS max_id
                FROM risk_scores
                GROUP BY wallet, asset_pair
            ) latest ON rs.id = latest.max_id
            WHERE NOT EXISTS (
                SELECT 1 FROM analyst_feedback af
                WHERE af.wallet = rs.wallet
                  AND af.asset_pair = rs.asset_pair
                  AND af.submitted_at >= ?
            )
            AND NOT EXISTS (
                SELECT 1 FROM case_assignments ca
                WHERE ca.wallet = rs.wallet
                  AND ca.asset_pair = rs.asset_pair
                  AND ca.status = 'assigned'
                  AND ca.lock_expires_at > ?
            )
            """,
            (today_start, now.isoformat()),
        ).fetchone()[0]

        # Expired/reclaimed locks (released status with a released_at)
        expired_count = conn.execute(
            """
            SELECT COUNT(*) FROM case_assignments
            WHERE status = 'released' AND released_at IS NOT NULL
            """,
        ).fetchone()[0]

    return {
        "avg_time_to_claim_seconds": round(avg_ttc, 1) if avg_ttc is not None else None,
        "avg_time_to_resolution_seconds": round(avg_ttr, 1) if avg_ttr is not None else None,
        "assigned_count": assigned_count,
        "unassigned_count": unassigned_count,
        "expired_reclaimed_count": expired_count,
    }
