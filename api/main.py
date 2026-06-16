"""Local read-only API for `RiskScore` records produced by `run_pipeline.py`.

This is a lightweight stand-in for the `ledgerlens-api` repo, useful for
local development and demos: it serves whatever has been written to the
local SQLite store (`detection.storage`) by `run_pipeline.py` or
`cli.py score`. `ledgerlens-api` will eventually own the canonical,
production version of these endpoints (`/score`, `/alerts`,
`/assets/risk-ranking`) — see README's "LedgerLens Organization" section.

Also exposes webhook subscriber management endpoints.

Run with:

    uvicorn api.main:app --reload
"""

import json
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from config.settings import settings
from detection.risk_score import RiskScore
from detection.storage import get_latest_scores
from detection.webhook_queue import get_dead_letters
from detection.webhook_registry import deactivate_subscriber, list_subscribers, register_subscriber

app = FastAPI(
    title="LedgerLens (local)",
    description="Local read-only API serving RiskScore records from the detection engine.",
    version="0.1.0",
)


class WebhookCreate(BaseModel):
    url: str
    secret: str
    min_score: int = 70
    wallet_filter: str | None = None
    asset_pair_filter: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/scores", response_model=list[RiskScore])
def list_scores(
    min_score: int = 0,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    benford_flag: bool | None = Query(
        default=None,
        description="Filter by the Benford detector flag when provided.",
    ),
    ml_flag: bool | None = Query(
        default=None,
        description="Filter by the machine-learning detector flag when provided.",
    ),
    sort_by: str = Query(
        default="score",
        pattern="^(score|confidence|timestamp)$",
        description="Sort descending by score, confidence, or timestamp.",
    ),
) -> list[RiskScore]:
    """Return latest scores filtered by score, flags, paging, and `sort_by` ordering."""
    scores = get_latest_scores(
        limit=limit,
        offset=offset,
        benford_flag=benford_flag,
        ml_flag=ml_flag,
        sort_by=sort_by,
    )
    return [s for s in scores if s.score >= min_score]



@app.get("/scores/{wallet}", response_model=list[RiskScore])
def wallet_scores(wallet: str) -> list[RiskScore]:
    """Return the latest score for `wallet` on each asset pair."""
    scores = get_latest_scores(wallet=wallet)
    if not scores:
        raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")
    return scores


@app.get("/alerts", response_model=list[RiskScore])
def alerts(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[RiskScore]:
    """Return scores at or above `settings.risk_score_threshold`."""
    scores = get_latest_scores(limit=limit, offset=offset)
    return [s for s in scores if s.score >= settings.risk_score_threshold]



@app.get("/assets/risk-ranking")
def asset_risk_ranking() -> list[dict]:
    """Return each asset pair ranked by its average wallet risk score (descending)."""
    scores = get_latest_scores()
    by_pair: dict[str, list[int]] = defaultdict(list)
    for s in scores:
        by_pair[s.asset_pair].append(s.score)

    ranking = [
        {"asset_pair": pair, "average_score": round(sum(values) / len(values), 2), "wallet_count": len(values)}
        for pair, values in by_pair.items()
    ]
    return sorted(ranking, key=lambda r: r["average_score"], reverse=True)


# ---------------------------------------------------------------------------
# Webhook subscriber management
# ---------------------------------------------------------------------------


@app.post("/webhooks", status_code=201)
def create_webhook(body: WebhookCreate) -> dict:
    """Register a new webhook subscriber."""
    try:
        subscriber_id = register_subscriber(
            url=body.url,
            secret=body.secret,
            min_score=body.min_score,
            wallet_filter=body.wallet_filter,
            asset_pair_filter=body.asset_pair_filter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"subscriber_id": subscriber_id}


@app.get("/webhooks")
def list_webhooks() -> list[dict]:
    """Return all active subscribers (secrets are masked)."""
    return [
        {
            "subscriber_id": s.subscriber_id,
            "url": s.url,
            "secret": s.masked_secret(),
            "min_score": s.min_score,
            "wallet_filter": ",".join(s.wallet_filter) if s.wallet_filter else None,
            "asset_pair_filter": ",".join(s.asset_pair_filter) if s.asset_pair_filter else None,
            "created_at": s.created_at,
        }
        for s in list_subscribers()
    ]


@app.delete("/webhooks/{subscriber_id}")
def delete_webhook(subscriber_id: str) -> dict:
    """Deactivate a webhook subscriber."""
    if not deactivate_subscriber(subscriber_id):
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return {"status": "deactivated"}


@app.get("/webhooks/dead-letters")
def dead_letters() -> list[dict]:
    """Return all deliveries that have permanently failed."""
    return [
        {
            "id": d.id,
            "subscriber_id": d.subscriber_id,
            "payload": json.loads(d.payload_json),
            "attempt_count": d.attempt_count,
            "last_error": d.last_error,
            "created_at": d.created_at,
        }
        for d in get_dead_letters()
    ]
