"""Analyst review store — persistence for analyst feedback and queue management.

Stores analyst verdicts on flagged wallets and provides the query layer for
the analyst review dashboard API (Issue #200).  Records are consumed by the
active learning loop via GET /analyst/feedback?since=<ISO_TIMESTAMP>.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Literal

from detection.storage import _connect, init_db

VerdictType = Literal["confirmed_wash", "false_positive", "needs_review"]

_VALID_VERDICTS: frozenset[str] = frozenset(
    ["confirmed_wash", "false_positive", "needs_review"]
)


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def submit_analyst_feedback(
    wallet: str,
    asset_pair: str,
    verdict: str,
    notes: str | None,
    analyst_key_hash: str,
    review_started_at: datetime | None = None,
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
        db_path: Override DB path (defaults to settings.db_path).

    Returns:
        The persisted feedback record as a dict.

    Raises:
        ValueError: If verdict is not one of the accepted values.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"Invalid verdict '{verdict}'. Must be one of: {sorted(_VALID_VERDICTS)}"
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
# Read path — queue, feedback export, stats
# ---------------------------------------------------------------------------


def get_analyst_queue(limit: int = 20, db_path: str | None = None) -> list[dict]:
    """Return top ``limit`` wallets awaiting analyst review, sorted by score descending.

    A wallet is "awaiting review" when it has a risk score >= threshold and has
    no analyst feedback submitted today.

    Returns a list of dicts with: wallet, asset_pair, score, last_scored_at,
    has_open_alert, already_reviewed_today.
    """
    init_db(db_path)

    today_start = datetime.now(timezone.utc).replace(
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
                ) AS reviewed_today
            FROM risk_scores rs
            INNER JOIN (
                SELECT wallet, asset_pair, MAX(id) AS max_id
                FROM risk_scores
                GROUP BY wallet, asset_pair
            ) latest ON rs.id = latest.max_id
            WHERE reviewed_today = 0
            ORDER BY rs.score DESC
            LIMIT ?
            """,
            (today_start, limit),
        ).fetchall()

    return [
        {
            "wallet": row[0],
            "asset_pair": row[1],
            "score": row[2],
            "last_scored_at": row[3],
            "reviewed_today": bool(row[4]),
        }
        for row in rows
    ]


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
        avg_review_time = avg_row[0]  # None if no records with review_started_at

    return {
        "cases_reviewed_today": today_count,
        "false_positive_rate_30d": round(fp_rate, 4),
        "avg_review_time_seconds": round(avg_review_time, 1) if avg_review_time is not None else None,
    }
