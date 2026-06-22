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
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings
from detection.amm_engine import pool_risk_from_trade_rows
from detection.feedback_store import ScoringFeedback, record_feedback
from detection.risk_score import RiskScore
from detection.storage import (
    get_alerts,
    get_circular_routes,
    get_drift_reports,
    get_latest_scores,
    get_liquidity_pool_trades,
    get_pair_correlations,
    get_retrain_runs,
    get_rings,
    get_shap_values,
)
from detection.dispute_store import submit_dispute, get_dispute, cast_vote
import sqlite3
from detection.governance import create_proposal, list_open_proposals, cast_proposal_vote
from detection.webhook_queue import get_dead_letters
from detection.webhook_registry import deactivate_subscriber, list_subscribers, register_subscriber

logger = logging.getLogger("ledgerlens.api")

_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{55}$")


def validate_stellar_address(wallet: str) -> None:
    """Validate that `wallet` is a valid Stellar account ID.

    Stellar account IDs are exactly 56 characters long, start with 'G', and contain
    only base32 characters (A-Z and 2-7).

    Raises:
        HTTPException: 400 with generic message if validation fails.
    """
    if not _STELLAR_ADDRESS_PATTERN.match(wallet):
        raise HTTPException(status_code=400, detail="Invalid Stellar wallet address format.")

# ---------------------------------------------------------------------------
# Model loading — done once at startup so request handlers stay fast.
# ---------------------------------------------------------------------------

_models: dict = {}


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Load trained models at startup; release nothing at shutdown."""
    global _models
    try:
        from detection.model_inference import load_models
        _models = load_models(settings.model_dir)
        logger.info("Loaded %d model(s) from %s", len(_models), settings.model_dir)
    except FileNotFoundError:
        logger.warning("No trained models found in %s — /explain will return 503", settings.model_dir)
        _models = {}
    yield


app = FastAPI(
    title="LedgerLens (local)",
    description="Local read-only API serving RiskScore records from the detection engine.",
    version="0.1.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    allow_credentials=False,
)


class WebhookCreate(BaseModel):
    url: str
    secret: str
    min_score: int = 70
    wallet_filter: str | None = None
    asset_pair_filter: str | None = None


class DisputeCreate(BaseModel):
    wallet: str
    asset_pair: str
    evidence_url: str | None = None


class VoteBody(BaseModel):
    voter_key_hash: str
    vote: str


@app.get("/health")
def health() -> JSONResponse:
    """Returns 200 when healthy, 503 when any component check fails.

    Checks:
    - DB connectivity: executes SELECT 1 via the existing _connect helper.
    - Model files: each expected .joblib file exists and is non-empty.

    The response body names every component but never leaks local filesystem
    paths — errors are logged server-side at ERROR level.
    """
    from detection.model_inference import _MODEL_FILENAMES
    from detection.storage import _connect

    status: dict[str, str] = {}
    healthy = True

    # --- DB check ---
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        status["db"] = "ok"
    except sqlite3.Error as exc:
        logger.error("Health check: DB connectivity failure: %s", exc)
        status["db"] = "error: database unreachable"
        healthy = False

    # --- Model files check (existence + non-zero size only; no deserialization) ---
    missing = [
        name
        for name, filename in _MODEL_FILENAMES.items()
        if not _model_file_ok(os.path.join(settings.model_dir, filename))
    ]
    if missing:
        status["models"] = f"missing: {', '.join(sorted(missing))}"
        healthy = False
    else:
        status["models"] = "ok"

    status["status"] = "ok" if healthy else "degraded"
    http_status = 200 if healthy else 503
    return JSONResponse(content=status, status_code=http_status)


def _model_file_ok(path: str) -> bool:
    """Return True iff `path` exists as a non-empty regular file."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


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
    # mark disputed flags
    with sqlite3.connect(settings.db_path) as conn:
        for s in scores:
            r = conn.execute(
                "SELECT 1 FROM score_disputes WHERE wallet = ? AND asset_pair = ? AND status = 'pending' LIMIT 1",
                (s.wallet, s.asset_pair),
            ).fetchone()
            s.disputed = bool(r)
    return [s for s in scores if s.score >= min_score]



@app.get("/scores/{wallet}/explain")
def explain_wallet_score(
    wallet: str,
    asset_pair: str = Query(..., description="Asset pair to explain, e.g. XLM/USDC"),
) -> list[dict]:
    """Return the top-5 SHAP feature contributions for ``wallet`` on ``asset_pair``.

    The wallet parameter must be a valid Stellar account ID (56 characters, starting
    with 'G', containing only base32 characters A-Z and 2-7).

    Response schema: list of ``{"feature": str, "shap_value": float}`` ordered
    by absolute SHAP contribution descending.

    - **200** — cache hit: returns up to 5 feature contributions.
    - **404** — no SHAP cache found for the given wallet / asset pair combination.
    - **503** — models were not loaded at startup (run the training pipeline first).
    """
    if not _models:
        raise HTTPException(status_code=503, detail="Models not loaded")

    validate_stellar_address(wallet)
    cached = get_shap_values(wallet=wallet, asset_pair=asset_pair)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SHAP cache found for wallet {wallet} on {asset_pair}",
        )
    return cached


@app.get("/scores/{wallet}", response_model=list[RiskScore])
def wallet_scores(wallet: str) -> list[RiskScore]:
    """Return the latest score for `wallet` on each asset pair.

    The wallet parameter must be a valid Stellar account ID (56 characters, starting
    with 'G', containing only base32 characters A-Z and 2-7).
    """
    validate_stellar_address(wallet)
    scores = get_latest_scores(wallet=wallet)
    if not scores:
        raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")
    with sqlite3.connect(settings.db_path) as conn:
        for s in scores:
            r = conn.execute(
                "SELECT 1 FROM score_disputes WHERE wallet = ? AND asset_pair = ? AND status = 'pending' LIMIT 1",
                (s.wallet, s.asset_pair),
            ).fetchone()
            s.disputed = bool(r)
    return scores


@app.get("/alerts")
def alerts(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    alert_type: str | None = Query(
        default=None,
        description="Filter typed manipulation alerts (e.g. SANDWICH_ATTACK). "
        "When omitted, returns risk scores at or above the threshold.",
    ),
):
    """Return manipulation alerts.

    Without `alert_type`, returns `RiskScore` records at or above
    `settings.risk_score_threshold` (legacy behaviour). With `alert_type`,
    returns stored typed alerts (see `detection.storage.AlertType`), such as
    `SANDWICH_ATTACK`, most recent first.
    """
    if alert_type is not None:
        return get_alerts(alert_type=alert_type, limit=limit, offset=offset)

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


@app.get("/rings")
def list_rings() -> list[dict]:
    """Return detected wash-trading rings from the latest pipeline run."""
    return get_rings()


@app.get("/correlations")
def list_correlations() -> list[dict]:
    """Return the most recent set of correlated asset pairs from the pipeline.

    Each entry includes the pair names, Spearman correlation coefficient,
    the method used, the count of shared wallets in burst windows, and the
    run timestamp.
    """
    return get_pair_correlations()


@app.get("/amm/pools/{pool_id}/risk")
def pool_risk(pool_id: str) -> dict:
    """Return pool-level round-trip ratio and trader concentration for `pool_id`.

    Based on AMM pool trades ingested by `run_pipeline.py` (see
    `ingestion.amm_loader`) and stored in `liquidity_pool_trades`.
    """
    rows = get_liquidity_pool_trades(pool_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No pool trades found for pool {pool_id}")
    risk = pool_risk_from_trade_rows(rows)
    return {"pool_id": pool_id, **risk}


@app.get("/path-payments/circular")
def circular_path_payments(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Return detected atomic circular path-payment routes, paginated."""
    return get_circular_routes(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Feedback ingestion — admin-key gated
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    wallet: str
    asset_pair: str
    ground_truth: int  # 1 = confirmed wash, 0 = confirmed clean
    scored_at: str     # ISO-8601 datetime of the scoring event


@app.post("/feedback", dependencies=[Depends(require_admin_key)])
def submit_feedback(body: FeedbackRequest) -> dict:
    """Record ground-truth feedback for a previously scored wallet/asset_pair.

    Looks up the stored per-model predictions from the ``risk_scores`` table
    for the matching ``wallet``, ``asset_pair``, and ``scored_at`` timestamp,
    then writes one :class:`~detection.feedback_store.ScoringFeedback` row per
    model (3 rows total).

    Returns ``{"recorded": 3}`` on success or 404 if no matching score is found.
    """
    from datetime import datetime, timezone

    from detection.storage import _connect, init_db

    try:
        scored_at_dt = datetime.fromisoformat(body.scored_at)
        if scored_at_dt.tzinfo is None:
            scored_at_dt = scored_at_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid scored_at: {exc}")

    # Ensure schema exists before querying
    init_db()

    with _connect() as conn:
        row = conn.execute(
            "SELECT score, shap_json FROM risk_scores "
            "WHERE wallet = ? AND asset_pair = ? AND timestamp = ? "
            "ORDER BY id DESC LIMIT 1",
            (body.wallet, body.asset_pair, scored_at_dt.isoformat()),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No score found for wallet={body.wallet} asset_pair={body.asset_pair} scored_at={body.scored_at}",
        )

    # We use the blended score / 100 as a proxy probability for each model
    # when per-model probabilities are not stored separately.
    blended_prob = row[0] / 100.0
    confirmed_at = datetime.now(timezone.utc)
    count = 0
    for model_name in ("random_forest", "xgboost", "lightgbm"):
        record_feedback(
            ScoringFeedback(
                wallet=body.wallet,
                asset_pair=body.asset_pair,
                model_name=model_name,
                predicted_probability=blended_prob,
                ground_truth=body.ground_truth,
                scored_at=scored_at_dt,
                confirmed_at=confirmed_at,
            )
        )
        count += 1

    return {"recorded": count}


# ---------------------------------------------------------------------------
# Model observability — drift reports and retrain runs (admin-key gated)
# ---------------------------------------------------------------------------


@app.get("/admin/drift-reports", dependencies=[Depends(require_admin_key)])
def drift_reports(limit: int = Query(default=50, ge=1, le=1000)) -> list[dict]:
    """Return the most recent drift checks recorded by `cli.py retrain-check`."""
    return get_drift_reports(limit=limit)


@app.get("/admin/robustness-report", dependencies=[Depends(require_admin_key)])
def robustness_report() -> dict:
    """Return the latest RobustnessReport from the database (admin only)."""
    from detection.storage import get_latest_robustness_report

    report = get_latest_robustness_report()
    if report is None:
        raise HTTPException(status_code=404, detail="No robustness report found")
    return report


@app.get("/admin/retrain-runs", dependencies=[Depends(require_admin_key)])
def retrain_runs(
    limit: int = Query(default=50, ge=1, le=1000),
    model_name: str | None = Query(default=None, description="Filter by model, e.g. random_forest"),
) -> list[dict]:
    """Return the most recent per-model retrain outcomes recorded by `cli.py retrain-check`."""
    return get_retrain_runs(limit=limit, model_name=model_name)


@app.get("/admin/federated/audit-log", dependencies=[Depends(require_admin_key)])
def federated_audit_log(
    limit: int = Query(default=50, ge=1, le=1000),
) -> list[dict]:
    """Return the most recent federated-round audit records (participant IDs are SHA-256 hashed)."""
    from detection.federated.audit import get_audit_records
    return get_audit_records(limit=limit)


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


# ------------------------------------------------------------------
# Disputes
# ------------------------------------------------------------------


@app.post("/disputes", status_code=201)
def create_dispute(body: DisputeCreate):
    try:
        dispute = submit_dispute(body.wallet, body.asset_pair, body.evidence_url)
    except ValueError as exc:
        # Rate limit or missing submission
        if "Rate limit" in str(exc):
            raise HTTPException(status_code=429, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    return dispute.dict()


@app.get("/disputes/{dispute_id}")
def read_dispute(dispute_id: str):
    d = get_dispute(dispute_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Dispute not found")
    # hide voter identities: return counts only
    approves = sum(1 for v in d.committee_votes if v.get("vote") == "approve")
    rejects = sum(1 for v in d.committee_votes if v.get("vote") == "reject")
    return {
        "dispute_id": d.dispute_id,
        "wallet": d.wallet,
        "asset_pair": d.asset_pair,
        "disputed_score": d.disputed_score,
        "soroban_tx_hash": d.soroban_tx_hash,
        "evidence_url": d.evidence_url,
        "submitted_at": d.submitted_at,
        "status": d.status,
        "votes": {"approve": approves, "reject": rejects},
        "resolved_at": d.resolved_at,
        "resolution": d.resolution,
    }


@app.post("/disputes/{dispute_id}/vote", dependencies=[Depends(require_admin_key)])
def vote_dispute(dispute_id: str, body: VoteBody):
    # validate voter_key_hash format
    if len(body.voter_key_hash) != 64:
        raise HTTPException(status_code=422, detail="voter_key_hash must be 64 hex chars")
    if body.vote not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="vote must be 'approve' or 'reject'")
    try:
        d = cast_vote(dispute_id, body.voter_key_hash, body.vote)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return d.dict()


# ------------------------------------------------------------------
# Governance
# ------------------------------------------------------------------


@app.get("/governance/proposals")
def get_proposals():
    return [p.dict() for p in list_open_proposals()]


class ProposalCreate(BaseModel):
    proposal_type: str
    proposed_value: str
    proposed_by_key_hash: str


@app.post("/governance/proposals", dependencies=[Depends(require_admin_key)])
def create_proposal_endpoint(body: ProposalCreate):
    try:
        p = create_proposal(body.proposal_type, body.proposed_value, body.proposed_by_key_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return p.dict()


class ProposalVote(BaseModel):
    voter_key_hash: str
    vote: str


@app.post("/governance/proposals/{proposal_id}/vote", dependencies=[Depends(require_admin_key)])
def vote_proposal(proposal_id: str, body: ProposalVote):
    try:
        p = cast_proposal_vote(proposal_id, body.voter_key_hash, body.vote)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return p.dict()

