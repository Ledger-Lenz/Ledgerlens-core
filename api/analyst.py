"""Analyst Review Dashboard API — Issue #200.

Provides a combined view of risk score, SHAP explanation, trade timeline,
ring membership, and analyst feedback capture for compliance analysts.

All endpoints are admin-key gated (X-LedgerLens-Admin-Key header).

Endpoints
---------
GET  /analyst/wallet/{wallet}           Combined review view for a wallet
POST /analyst/wallet/{wallet}/feedback  Submit analyst verdict
GET  /analyst/queue                     Top 20 wallets awaiting review
GET  /analyst/stats                     Aggregate review statistics
GET  /analyst/feedback                  Export feedback since ISO timestamp (active learning)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings
from detection.analyst_store import (
    get_analyst_feedback_since,
    get_analyst_queue,
    get_analyst_stats,
    submit_analyst_feedback,
)
from detection.storage import get_latest_scores, get_shap_values, get_rings, init_db

router = APIRouter(prefix="/analyst", tags=["analyst"])

_WALLET_PATTERN = __import__("re").compile(r"^G[A-Z2-7]{55}$")


def _validate_wallet(wallet: str) -> None:
    if not _WALLET_PATTERN.match(wallet):
        raise HTTPException(status_code=400, detail="Invalid Stellar wallet address format.")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _get_score_trend(wallet: str, db_path: str | None = None) -> list[dict]:
    """Return the last 30 days of score history for ``wallet``."""
    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with sqlite3.connect(db_path or settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT asset_pair, score, timestamp
            FROM risk_scores
            WHERE wallet = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 200
            """,
            (wallet, thirty_ago),
        ).fetchall()
    return [{"asset_pair": r[0], "score": r[1], "timestamp": r[2]} for r in rows]


def _get_trade_timeline(wallet: str, db_path: str | None = None) -> list[dict]:
    """Return the last 30 days of scored trades for ``wallet``."""
    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with sqlite3.connect(db_path or settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT asset_pair, score, confidence, benford_flag, ml_flag, timestamp
            FROM risk_scores
            WHERE wallet = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 100
            """,
            (wallet, thirty_ago),
        ).fetchall()
    return [
        {
            "asset_pair": r[0],
            "score": r[1],
            "confidence": r[2],
            "benford_flag": bool(r[3]),
            "ml_flag": bool(r[4]),
            "timestamp": r[5],
        }
        for r in rows
    ]


def _get_ring_membership(wallet: str, db_path: str | None = None) -> list[dict]:
    """Return wash-trading rings that include ``wallet``."""
    rings = get_rings(db_path=db_path)
    return [r for r in rings if wallet in (r.get("accounts") or [])]


def _get_open_alerts(wallet: str, db_path: str | None = None) -> list[dict]:
    """Return open typed alerts for ``wallet``."""
    with sqlite3.connect(db_path or settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT alert_type, asset_pair, detail_json, timestamp
            FROM alerts
            WHERE wallet = ?
            ORDER BY timestamp DESC
            LIMIT 50
            """,
            (wallet,),
        ).fetchall()
    return [
        {
            "alert_type": r[0],
            "asset_pair": r[1],
            "detail": json.loads(r[2]) if r[2] else {},
            "timestamp": r[3],
        }
        for r in rows
    ]


def _get_shap_top10(wallet: str, asset_pair: str | None) -> list[dict]:
    """Return top-10 SHAP feature contributions for the wallet's most-scored pair."""
    if asset_pair:
        shap = get_shap_values(wallet=wallet, asset_pair=asset_pair)
    else:
        scores = get_latest_scores(wallet=wallet)
        if not scores:
            return []
        top_score = max(scores, key=lambda s: s.score)
        shap = get_shap_values(wallet=wallet, asset_pair=top_score.asset_pair)

    if not shap:
        return []
    sorted_shap = sorted(shap, key=lambda x: abs(x.get("shap_value", 0)), reverse=True)
    return sorted_shap[:10]


# ---------------------------------------------------------------------------
# GET /analyst/wallet/{wallet}
# ---------------------------------------------------------------------------


@router.get("/wallet/{wallet}", dependencies=[Depends(require_admin_key)])
def analyst_wallet_view(
    wallet: str,
    asset_pair: str | None = Query(
        default=None,
        description="Asset pair to focus on (optional; defaults to highest-scored pair)",
    ),
) -> dict:
    """Return combined analyst view for ``wallet``.

    Response sections:
    1. current_score      — latest RiskScore record
    2. shap_top_10        — top-10 SHAP feature contributions
    3. trade_timeline     — last 30 days of scoring events
    4. ring_membership    — wash-trading rings containing this wallet
    5. score_trend        — historical score values (last 30 days)
    6. open_alerts        — typed manipulation alerts for this wallet
    """
    _validate_wallet(wallet)
    init_db()

    scores = get_latest_scores(wallet=wallet)
    if not scores:
        raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")

    # Pick the highest-scoring pair as the default focus
    top_score = max(scores, key=lambda s: s.score)
    focus_pair = asset_pair or top_score.asset_pair

    current_score = next((s for s in scores if s.asset_pair == focus_pair), top_score)

    return {
        "wallet": wallet,
        "focus_asset_pair": focus_pair,
        "current_score": current_score.model_dump(),
        "shap_top_10": _get_shap_top10(wallet, focus_pair),
        "trade_timeline": _get_trade_timeline(wallet),
        "ring_membership": _get_ring_membership(wallet),
        "score_trend": _get_score_trend(wallet),
        "open_alerts": _get_open_alerts(wallet),
    }


# ---------------------------------------------------------------------------
# POST /analyst/wallet/{wallet}/feedback
# ---------------------------------------------------------------------------


class AnalystFeedbackRequest(BaseModel):
    verdict: str  # confirmed_wash | false_positive | needs_review
    notes: str | None = None
    analyst_key_hash: str = "anonymous"
    review_started_at: str | None = None  # ISO-8601


@router.post("/wallet/{wallet}/feedback", dependencies=[Depends(require_admin_key)], status_code=201)
def submit_feedback(wallet: str, body: AnalystFeedbackRequest) -> dict:
    """Capture analyst verdict for ``wallet``.

    The verdict is stored in ``analyst_feedback`` and is available to the
    active learning loop via ``GET /analyst/feedback?since=<ISO_TIMESTAMP>``.

    Accepted verdicts: ``confirmed_wash``, ``false_positive``, ``needs_review``.
    """
    _validate_wallet(wallet)

    review_started_at: datetime | None = None
    if body.review_started_at:
        try:
            review_started_at = datetime.fromisoformat(body.review_started_at)
            if review_started_at.tzinfo is None:
                review_started_at = review_started_at.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid review_started_at: {exc}")

    # Default asset_pair from the wallet's highest-scored pair
    scores = get_latest_scores(wallet=wallet)
    asset_pair = max(scores, key=lambda s: s.score).asset_pair if scores else "unknown"

    try:
        record = submit_analyst_feedback(
            wallet=wallet,
            asset_pair=asset_pair,
            verdict=body.verdict,
            notes=body.notes,
            analyst_key_hash=body.analyst_key_hash,
            review_started_at=review_started_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return record


# ---------------------------------------------------------------------------
# GET /analyst/queue
# ---------------------------------------------------------------------------


@router.get("/queue", dependencies=[Depends(require_admin_key)])
def analyst_queue(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Return the top ``limit`` wallets awaiting analyst review, sorted by score desc.

    A wallet is "awaiting review" when it has a risk score >= threshold and
    has not been reviewed today.
    """
    init_db()
    return get_analyst_queue(limit=limit)


# ---------------------------------------------------------------------------
# GET /analyst/stats
# ---------------------------------------------------------------------------


@router.get("/stats", dependencies=[Depends(require_admin_key)])
def analyst_stats() -> dict:
    """Return aggregate analyst review statistics.

    Response fields:
    - cases_reviewed_today: int
    - false_positive_rate_30d: float (0.0–1.0)
    - avg_review_time_seconds: float | None
    """
    init_db()
    return get_analyst_stats()


# ---------------------------------------------------------------------------
# GET /analyst/feedback  (active learning export)
# ---------------------------------------------------------------------------


@router.get("/feedback", dependencies=[Depends(require_admin_key)])
def analyst_feedback_export(
    since: str = Query(
        ...,
        description="ISO-8601 timestamp; returns all feedback submitted at or after this time",
    ),
) -> list[dict]:
    """Export analyst feedback records for the active learning loop (Issue #052).

    Consumed by the retraining pipeline to ingest human labels.  Returns all
    feedback submitted on or after ``since`` in ascending chronological order.

    Example: ``GET /analyst/feedback?since=2026-06-01T00:00:00Z``
    """
    try:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid since timestamp: {exc}")

    init_db()
    return get_analyst_feedback_since(since=since_dt)
