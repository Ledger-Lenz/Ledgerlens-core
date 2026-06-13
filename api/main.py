"""Local read-only API for `RiskScore` records produced by `run_pipeline.py`.

This is a lightweight stand-in for the `ledgerlens-api` repo, useful for
local development and demos: it serves whatever has been written to the
local SQLite store (`detection.storage`) by `run_pipeline.py` or
`cli.py score`. `ledgerlens-api` will eventually own the canonical,
production version of these endpoints (`/score`, `/alerts`,
`/assets/risk-ranking`) — see README's "LedgerLens Organization" section.

Run with:

    uvicorn api.main:app --reload
"""

from collections import defaultdict

from fastapi import FastAPI, HTTPException

from config.settings import settings
from detection.risk_score import RiskScore
from detection.storage import get_latest_scores

app = FastAPI(
    title="LedgerLens (local)",
    description="Local read-only API serving RiskScore records from the detection engine.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/scores", response_model=list[RiskScore])
def list_scores(min_score: int = 0) -> list[RiskScore]:
    """Return the latest score for each (wallet, asset_pair), optionally filtered by `min_score`."""
    scores = get_latest_scores()
    return [s for s in scores if s.score >= min_score]


@app.get("/scores/{wallet}", response_model=list[RiskScore])
def wallet_scores(wallet: str) -> list[RiskScore]:
    """Return the latest score for `wallet` on each asset pair."""
    scores = get_latest_scores(wallet=wallet)
    if not scores:
        raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")
    return scores


@app.get("/alerts", response_model=list[RiskScore])
def alerts() -> list[RiskScore]:
    """Return scores at or above `settings.risk_score_threshold`."""
    scores = get_latest_scores()
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
