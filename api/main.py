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
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from api.auth import require_admin_key, require_compliance_key
from api.admin_router import router as admin_router
from api.export_router import router as export_router
from api.batch_router import router as batch_router
from config.settings import settings
from detection.tracing import (
    configure_tracing,
    extract_context_from_headers,
    get_tracer,
    start_span,
)
from detection.amm_engine import pool_risk_from_trade_rows
from detection.feedback_store import ScoringFeedback, record_feedback
from detection.risk_score import RiskScore
from detection.counterfactual_engine import generate_counterfactuals
from detection.counterfactual_translator import translate_counterfactual
from detection.storage import (
    get_alerts,
    get_bridge_transfer_history,
    get_bridge_transfers,
    get_circular_routes,
    get_drift_reports,
    get_feature_vector,
    get_latest_scores,
    get_liquidity_pool_trades,
    get_pair_correlations,
    get_retrain_runs,
    get_rings,
    get_shap_values,
)
from detection.dispute_store import submit_dispute, get_dispute, cast_vote
from detection.governance import create_proposal, list_open_proposals, cast_proposal_vote
from detection.webhook_queue import get_dead_letters
from detection.webhook_registry import deactivate_subscriber, list_subscribers, register_subscriber

logger = logging.getLogger("ledgerlens.api")

_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{55}$")

# ---------------------------------------------------------------------------
# Simple in-process IP rate limiter for the causal-explanation endpoint.
# Limit: 10 requests per minute per IP (token-bucket style).
# ---------------------------------------------------------------------------
_CAUSAL_RATE_LIMIT = 10         # max requests per window
_CAUSAL_RATE_WINDOW = 60.0      # window size in seconds
_causal_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_causal_rate_limit(client_ip: str) -> None:
    """Raise HTTP 429 if ``client_ip`` has exceeded the causal-explanation rate limit.

    Uses a sliding window: only timestamps within the last 60 seconds are counted.
    """
    now = time.monotonic()
    bucket = _causal_rate_buckets[client_ip]
    # Evict timestamps outside the window
    _causal_rate_buckets[client_ip] = [t for t in bucket if now - t < _CAUSAL_RATE_WINDOW]
    if len(_causal_rate_buckets[client_ip]) >= _CAUSAL_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: causal-explanation endpoint allows "
                f"{_CAUSAL_RATE_LIMIT} requests per minute per IP."
            ),
        )
    _causal_rate_buckets[client_ip].append(now)


# ---------------------------------------------------------------------------
# Causal engine singleton — fitted lazily on first request.
# ---------------------------------------------------------------------------
_causal_engine = None
_causal_engine_lock = __import__("threading").Lock()


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
    """Load trained models at startup; close WebSocket connections at shutdown."""
    global _models
    configure_tracing()
    try:
        from detection.model_inference import load_models
        _models = load_models(settings.model_dir)
        logger.info("Loaded %d model(s) from %s", len(_models), settings.model_dir)
    except (FileNotFoundError, RuntimeError) as e:
        logger.warning("No trained models loaded from %s (%s) — /explain will return 503", settings.model_dir, e)
        _models = {}
    yield
    from api.ws_router import manager as _ws_manager
    await _ws_manager.close_all()


app = FastAPI(
    title="LedgerLens (local)",
    description="Local read-only API serving RiskScore records from the detection engine.",
    version="1.0.0",
    lifespan=_lifespan,
)


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_operational_error_handler(request: Request, exc: sqlite3.OperationalError):
    """Return 503 with Retry-After when SQLite is locked or unavailable."""
    logger.error("SQLite operational error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please retry."},
        headers={"Retry-After": "5"},
    )


app.include_router(analyst_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    allow_credentials=False,
)

from api.ws_router import router as _ws_router  # noqa: E402
app.include_router(_ws_router)

app.include_router(admin_router)


app.include_router(batch_router)


app.include_router(export_router)


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


@v1_router.get("/health")
def health() -> JSONResponse:
    """Returns 200 when healthy, 503 when any hard-failure component check fails.

    Checks:
    - DB connectivity: executes SELECT 1 via the existing _connect helper.
    - Model files: each expected .joblib file exists and is non-empty.
    - Circuit breakers: Horizon ingestion and the Redis feature store each
      have a breaker (see `utils.circuit_breaker`). An OPEN/HALF_OPEN
      circuit marks the response "degraded" but keeps returning 200 (the
      service is still serving traffic in a reduced-functionality state,
      not failed) — only DB/model failures return 503.

    The response body names every component but never leaks local filesystem
    paths — errors are logged server-side at ERROR level.
    """
    from detection.model_inference import _MODEL_FILENAMES
    from detection.storage import _connect
    from ingestion.horizon_streamer import horizon_circuit
    from utils.circuit_breaker import CircuitState

    status: dict[str, object] = {}
    healthy = True
    degraded = False

    # --- DB check ---
    with start_span("db.health_check"):
        try:
            with _connect() as conn:
                conn.execute("SELECT 1")
            status["db"] = "ok"
        except sqlite3.Error as exc:
            logger.error("Health check: DB connectivity failure: %s", exc)
            status["db"] = "error: database unreachable"
            healthy = False

    # --- Model files check (existence + non-zero size only; no deserialization) ---
    with start_span("models.health_check"):
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

    # --- Circuit breakers (open/half-open => degraded, not failed) ---
    try:
        feature_store_circuit_state = _get_health_feature_store().circuit_state
    except Exception as exc:
        logger.error("Health check: feature store circuit lookup failed: %s", exc)
        feature_store_circuit_state = CircuitState.OPEN.value
    circuits = {
        "horizon": horizon_circuit.state.value,
        "feature_store_redis": feature_store_circuit_state,
    }
    status["circuits"] = circuits
    if any(state != CircuitState.CLOSED.value for state in circuits.values()):
        degraded = True

    if healthy:
        status["status"] = "degraded" if degraded else "ok"
        http_status = 200
    else:
        status["status"] = "degraded"
        http_status = 503
    return JSONResponse(content=status, status_code=http_status)


def _model_file_ok(path: str) -> bool:
    """Return True iff `path` exists as a non-empty regular file."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


@v1_router.get("/scores", response_model=list[RiskScore])
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



@v1_router.get("/scores/{wallet}/explain")
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
    with start_span("redis.shap_lookup", attributes={"wallet": wallet, "asset_pair": asset_pair}):
        cached = get_shap_values(wallet=wallet, asset_pair=asset_pair)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SHAP cache found for wallet {wallet} on {asset_pair}",
        )
    return cached


class RateLimiterStatus(BaseModel):
    configured_rate: float
    current_rate: float
    bucket_level: float
    backpressure_active: bool
    queue_size: int
    last_429_at: Optional[datetime] = None


@app.get(
    "/stream/rate-limiter",
    response_model=RateLimiterStatus,
    dependencies=[Depends(require_admin_key)],
)
def rate_limiter_status() -> RateLimiterStatus:
    """Return current rate limiter and backpressure state.

    Requires the ``X-LedgerLens-Admin-Key`` header.  Returns 503 if no
    streamer is currently registered.
    """
    bucket = _stream_rate_limiter_state.get("bucket")
    if bucket is None:
        raise HTTPException(status_code=503, detail="Rate limiter not active (no streamer running)")

    bp = _stream_rate_limiter_state.get("backpressure")
    adaptive = _stream_rate_limiter_state.get("adaptive")
    last_429_dt: Optional[datetime] = None
    if adaptive and adaptive.last_429_at is not None:
        last_429_dt = datetime.fromtimestamp(adaptive.last_429_at, tz=timezone.utc)

    return RateLimiterStatus(
        configured_rate=bucket.current_rate,
        current_rate=bucket.current_rate,
        bucket_level=bucket.bucket_level,
        backpressure_active=bp.is_paused if bp else False,
        queue_size=bp.queue_size if bp else 0,
        last_429_at=last_429_dt,
    )


_COUNTERFACTUAL_TIMEOUT_SECONDS = 5
_counterfactual_executor = ThreadPoolExecutor(max_workers=4)


@v1_router.get("/scores/{wallet}/counterfactual")
def wallet_counterfactual(
    wallet: str,
    asset_pair: str = Query(..., description="Asset pair to generate counterfactuals for, e.g. XLM/USDC"),
    n: int = Query(default=3, ge=1, le=5),
    target_score: int | None = Query(default=None, ge=0, le=99),
) -> dict:
    """Return up to `n` minimal feature changes that would drop `wallet`'s score below `target_score`.

    Looks up `wallet`'s most recently cached feature vector for `asset_pair`
    (saved by `run_pipeline.py`), then searches for feasible counterfactuals
    with `detection.counterfactual_engine.generate_counterfactuals` and
    translates each into plain English. Only feature deltas and human-readable
    text are returned -- never model weights or internal probability outputs.

    The search is hard-capped at 5 seconds; if it doesn't finish in time this
    still returns 200 with an empty `counterfactuals` list rather than hanging
    indefinitely (unbounded optimisation search is a denial-of-service vector).

    - **404** — no cached feature vector for the given wallet / asset pair.
    - **422** — `target_score` outside `[0, 99]` or `n` outside `[1, 5]`.
    - **503** — models were not loaded at startup.
    """
    if not _models:
        raise HTTPException(status_code=503, detail="Models not loaded")

    validate_stellar_address(wallet)
    feature_vector = get_feature_vector(wallet, asset_pair)
    if feature_vector is None:
        raise HTTPException(
            status_code=404, detail=f"No cached feature vector for wallet {wallet} on {asset_pair}"
        )

    from detection.model_inference import score_feature_vector

    resolved_target_score = target_score if target_score is not None else settings.risk_score_threshold - 1
    with start_span("model.inference", attributes={"wallet": wallet}):
        current_probability, _confidence = score_feature_vector(_models, feature_vector)
    current_score = round(current_probability * 100)

    future = _counterfactual_executor.submit(
        generate_counterfactuals, feature_vector, _models, n, resolved_target_score
    )
    try:
        counterfactuals = future.result(timeout=_COUNTERFACTUAL_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        counterfactuals = []

    return {
        "wallet": wallet,
        "asset_pair": asset_pair,
        "current_score": current_score,
        "target_score": resolved_target_score,
        "counterfactuals": [
            {
                "rank": rank,
                "distance": cf["distance"],
                "predicted_score": cf["predicted_score"],
                "feature_deltas": cf["feature_deltas"],
                "human_readable": translate_counterfactual(cf["feature_deltas"]),
            }
            for rank, cf in enumerate(counterfactuals, start=1)
        ],
    }


@v1_router.get("/scores/{wallet}")
def wallet_scores(wallet: str) -> dict:
    """Return the latest score for `wallet` on each asset pair.

    When the wallet has known EVM counterparts (bridge transfer records in the
    database), the response includes a ``"cross_chain_links"`` field listing
    the linked EVM wallets and the chain they were last seen on.  EVM RPC
    endpoint URLs are never exposed in this response.
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

    transfers = get_bridge_transfers(stellar_wallet=wallet, since_days=90)
    seen: dict[tuple, dict] = {}
    for t in transfers:
        key = (t.chain, t.evm_wallet)
        if key not in seen or t.timestamp.isoformat() > seen[key]["last_bridge_at"]:
            seen[key] = {
                "chain": t.chain,
                "evm_wallet": t.evm_wallet,
                "last_bridge_at": t.timestamp.isoformat(),
            }
    cross_chain_links = list(seen.values())

    return {
        "scores": [s.model_dump() for s in scores],
        "cross_chain_links": cross_chain_links,
    }


@v1_router.get("/wallets/{wallet}/cross-chain")
def wallet_cross_chain(wallet: str) -> list[dict]:
    """Return the full bridge transfer history for ``wallet``.

    ``amount_usd_estimate`` values are derived from on-chain oracle prices and
    may be manipulated — treat them as estimates only.
    """
    history = get_bridge_transfer_history(stellar_wallet=wallet)
    if not history:
        raise HTTPException(status_code=404, detail=f"No bridge transfer history for wallet {wallet}")
    return history


@v1_router.get("/alerts")
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



@v1_router.get("/assets/risk-ranking")
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


@v1_router.get("/rings")
def list_rings() -> list[dict]:
    """Return detected wash-trading rings from the latest pipeline run."""
    return get_rings()


@v1_router.get("/correlations")
def list_correlations() -> list[dict]:
    """Return the most recent set of correlated asset pairs from the pipeline.

    Each entry includes the pair names, Spearman correlation coefficient,
    the method used, the count of shared wallets in burst windows, and the
    run timestamp.
    """
    return get_pair_correlations()


@v1_router.get("/amm/pools/{pool_id}/risk")
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


@v1_router.get("/path-payments/circular")
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


@v1_router.post("/feedback", dependencies=[Depends(require_admin_key)])
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


@v1_router.get("/admin/drift-reports", dependencies=[Depends(require_admin_key)])
def drift_reports(limit: int = Query(default=50, ge=1, le=1000)) -> list[dict]:
    """Return the most recent drift checks recorded by `cli.py retrain-check`."""
    return get_drift_reports(limit=limit)


@v1_router.get("/admin/robustness-report", dependencies=[Depends(require_admin_key)])
def robustness_report() -> dict:
    """Return the latest RobustnessReport from the database (admin only)."""
    from detection.storage import get_latest_robustness_report

    report = get_latest_robustness_report()
    if report is None:
        raise HTTPException(status_code=404, detail="No robustness report found")
    return report


@v1_router.get("/model/robustness")
def model_robustness() -> dict:
    """Return live red team robustness metrics for the current model.

    Summarises the continuous adversarial loop (`detection.red_team`):
    ``evasion_rate_24h``, ``mean_generations_to_evade``, and ``hardening_delta``
    (change in evasion rate before/after the most recent retrain).
    """
    from detection.robustness_eval import live_robustness_metrics

    return live_robustness_metrics()


@v1_router.get("/admin/retrain-runs", dependencies=[Depends(require_admin_key)])
def retrain_runs(
    limit: int = Query(default=50, ge=1, le=1000),
    model_name: str | None = Query(default=None, description="Filter by model, e.g. random_forest"),
) -> list[dict]:
    """Return the most recent per-model retrain outcomes recorded by `cli.py retrain-check`."""
    return get_retrain_runs(limit=limit, model_name=model_name)


@v1_router.get("/admin/federated/audit-log", dependencies=[Depends(require_admin_key)])
def federated_audit_log(
    limit: int = Query(default=50, ge=1, le=1000),
) -> list[dict]:
    """Return the most recent federated-round audit records (participant IDs are SHA-256 hashed)."""
    from detection.federated.audit import get_audit_records
    return get_audit_records(limit=limit)


@app.get("/admin/namespaces", dependencies=[Depends(require_admin_key)])
def admin_namespaces() -> list[dict]:
    """Return every namespace with per-table record counts.

    Admin-only (requires the ``LEDGERLENS_ADMIN_API_KEY`` header).
    Gated by `require_admin_key` — the admin wildcard API key is
    required to see cross-namespace data.
    """
    return list_namespaces()


# ---------------------------------------------------------------------------
# Model weights
# ---------------------------------------------------------------------------


@v1_router.get("/model/weights")
def model_weights() -> JSONResponse:
    """Return current ensemble classifier weights from the adaptive reweighter."""
    from detection.adaptive_reweighter import (
        ThompsonSamplingReweighter,
        _CLASSIFIER_NAMES,
        get_global_reweighter,
        load_state,
    )

    rw = get_global_reweighter() or load_state()
    if rw is None:
        rw = ThompsonSamplingReweighter(n_classifiers=len(_CLASSIFIER_NAMES))

    weights = rw.current_weights()
    return JSONResponse({
        "classifiers": [
            {
                "name": name,
                "alpha": float(rw.alphas[i]),
                "beta": float(rw.betas[i]),
                "weight": weights[name],
            }
            for i, name in enumerate(_CLASSIFIER_NAMES)
        ],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Webhook subscriber management
# ---------------------------------------------------------------------------


@v1_router.post("/webhooks", status_code=201)
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


@v1_router.get("/webhooks")
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


@v1_router.delete("/webhooks/{subscriber_id}")
def delete_webhook(subscriber_id: str) -> dict:
    """Deactivate a webhook subscriber."""
    if not deactivate_subscriber(subscriber_id):
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return {"status": "deactivated"}


@v1_router.get("/webhooks/dead-letters")
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


@v1_router.post("/disputes", status_code=201)
def create_dispute(body: DisputeCreate):
    try:
        dispute = submit_dispute(body.wallet, body.asset_pair, body.evidence_url)
    except ValueError as exc:
        # Rate limit or missing submission
        if "Rate limit" in str(exc):
            raise HTTPException(status_code=429, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    return dispute.dict()


@v1_router.get("/disputes/{dispute_id}")
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


@v1_router.post("/disputes/{dispute_id}/vote", dependencies=[Depends(require_admin_key)])
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
# ZK Commitment endpoints (#147)
# ------------------------------------------------------------------


@v1_router.get("/governance/proposals")
def get_proposals():
    return [p.dict() for p in list_open_proposals()]


class LegacyProposalCreate(BaseModel):
    proposal_type: str
    proposed_value: str
    proposed_by_key_hash: str


@v1_router.post("/governance/proposals", dependencies=[Depends(require_admin_key)])
def create_proposal_endpoint(body: ProposalCreate):
    try:
        p = create_proposal(body.proposal_type, body.proposed_value, body.proposed_by_key_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return p.dict()


class LegacyProposalVote(BaseModel):
    voter_key_hash: str
    vote: str


@v1_router.post("/governance/proposals/{proposal_id}/vote", dependencies=[Depends(require_admin_key)])
def vote_proposal(proposal_id: str, body: ProposalVote):
    try:
        p = cast_proposal_vote(proposal_id, body.voter_key_hash, body.vote)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return p.dict()


# ------------------------------------------------------------------
# Regulatory compliance export layer
#
# These endpoints emit FATF Travel-Rule / SAR evidence and are gated behind the
# dedicated `compliance:read` scope (see api.auth.require_compliance_key). They
# are excluded from the public OpenAPI schema (include_in_schema=False) so they
# never surface on the unauthenticated /docs page.
# ------------------------------------------------------------------


class SARPackageRequest(BaseModel):
    wallet: str
    start_date: str
    end_date: str


@v1_router.get(
    "/compliance/ivms/{wallet}",
    dependencies=[Depends(require_compliance_key)],
    include_in_schema=False,
)
def compliance_ivms(wallet: str) -> dict:
    """Return the IVMS 101 risk-augmentation block for ``wallet``."""
    from dataclasses import asdict

    from detection.compliance_exporter import build_ivms_risk_field

    validate_stellar_address(wallet)
    return asdict(build_ivms_risk_field(wallet))


@v1_router.post(
    "/compliance/sar-package",
    dependencies=[Depends(require_compliance_key)],
    include_in_schema=False,
)
def compliance_sar_package(body: SARPackageRequest) -> FileResponse:
    """Generate a SAR evidence ZIP for a wallet and return it as a download."""
    import tempfile

    from detection.compliance_exporter import generate_sar_package

    validate_stellar_address(body.wallet)
    output_dir = tempfile.mkdtemp(prefix="ledgerlens_sar_")
    zip_path = generate_sar_package(
        wallet=body.wallet,
        start_date=body.start_date,
        end_date=body.end_date,
        output_dir=output_dir,
    )
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=os.path.basename(zip_path),
    )


@v1_router.get(
    "/compliance/audit-trail/{wallet}",
    dependencies=[Depends(require_compliance_key)],
    include_in_schema=False,
)
def compliance_audit_trail(wallet: str) -> list[dict]:
    """Return the full timestamped audit log for ``wallet`` (for legal hold)."""
    from detection.compliance_exporter import get_audit_trail

    validate_stellar_address(wallet)
    return get_audit_trail(wallet)


# ---------------------------------------------------------------------------
# Causal explanation endpoint
# ---------------------------------------------------------------------------

# Valid feature names accepted by the feature_override parameter.
_CAUSAL_FEATURE_NAMES_SET: frozenset[str] = frozenset([
    "wash_ring_membership",
    "round_trip_trade_frequency",
    "chi_sq_24h",
    "cycle_volume_ratio",
    "volume_to_unique_counterparty_ratio",
    "network_centrality",
    "account_age_days",
    "gnn_wash_ring_prob",
])

# Value range for feature overrides (validated for security).
_FEATURE_OVERRIDE_MIN = -1000.0
_FEATURE_OVERRIDE_MAX = 1000.0

# Refutation gate: if more than this many features have placebo p-value < 0.05,
# refuse to serve the ATE table and return 503.
_MAX_FAILING_REFUTATIONS = 3


class CausalExplanationResponse(BaseModel):
    """Response schema for GET /scores/{wallet}/causal-explanation."""

    wallet: str
    current_score: int
    feature_ate_table: dict[str, float]
    top_causal_features: list[tuple[str, float]]
    counterfactual_score: Optional[float]
    coverage_note: str


def _parse_feature_override(raw: str | None) -> tuple[str, float] | None:
    """Parse and strictly validate a ``feature=value`` override string.

    Returns ``(feature_name, value)`` on success or raises ``HTTPException``
    with status 422 on any validation failure.

    Security requirements:
    - Feature name must be a known observable feature (not arbitrary user input).
    - Value must be a finite float within [-1000, 1000].
    """
    if raw is None:
        return None
    if "=" not in raw:
        raise HTTPException(
            status_code=422,
            detail="feature_override must be in 'feature=value' format (e.g. 'wash_ring_membership=0.0').",
        )
    feature, _, value_str = raw.partition("=")
    feature = feature.strip()
    value_str = value_str.strip()

    if feature not in _CAUSAL_FEATURE_NAMES_SET:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown feature '{feature}'. "
                f"Valid features: {sorted(_CAUSAL_FEATURE_NAMES_SET)}"
            ),
        )
    try:
        value = float(value_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Feature override value '{value_str}' is not a valid number.",
        )
    import math
    if not math.isfinite(value):
        raise HTTPException(
            status_code=422,
            detail="Feature override value must be a finite number.",
        )
    if value < _FEATURE_OVERRIDE_MIN or value > _FEATURE_OVERRIDE_MAX:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Feature override value {value} is out of range "
                f"[{_FEATURE_OVERRIDE_MIN}, {_FEATURE_OVERRIDE_MAX}]."
            ),
        )
    return feature, value


def _get_or_fit_causal_engine():
    """Return the global CausalEngine, fitting lazily from stored scores if needed.

    Thread-safe via a module-level lock.  Returns None when insufficient data
    is available (< CAUSAL_MIN_SAMPLE_SIZE scored wallets in the database).
    """
    global _causal_engine
    if _causal_engine is not None and _causal_engine.is_fitted():
        return _causal_engine

    with _causal_engine_lock:
        if _causal_engine is not None and _causal_engine.is_fitted():
            return _causal_engine

        try:
            from detection.causal_engine import CausalEngine, build_causal_dag, OBSERVABLE_FEATURE_NODES
            from detection.storage import _connect
            import os

            min_sample = int(os.getenv("CAUSAL_MIN_SAMPLE_SIZE", "500"))
            method = os.getenv("CAUSAL_ESTIMATION_METHOD", "backdoor.linear_regression")
            refutation_runs = int(os.getenv("CAUSAL_REFUTATION_RUNS", "100"))
            model_version = os.getenv("LEDGERLENS_MODEL_VERSION", "default")

            with _connect() as conn:
                rows = conn.execute(
                    "SELECT wallet, asset_pair, score, shap_json FROM risk_scores "
                    "ORDER BY id DESC LIMIT 5000"
                ).fetchall()

            if len(rows) < min_sample:
                logger.warning(
                    "Causal engine: only %d scored wallets available (minimum %d). "
                    "Returning None.",
                    len(rows),
                    min_sample,
                )
                return None

            # Build a DataFrame from stored scores + shap_json feature proxies
            records = []
            for wallet_addr, asset_pair, score, shap_json_str in rows:
                record: dict = {"risk_score": float(score)}
                if shap_json_str:
                    try:
                        shap_data = json.loads(shap_json_str)
                        for item in shap_data:
                            feat = item.get("feature", "")
                            if feat in _CAUSAL_FEATURE_NAMES_SET:
                                record[feat] = float(item.get("shap_value", 0.0))
                    except Exception:
                        pass
                records.append(record)

            import pandas as pd
            df = pd.DataFrame(records)
            for feat in OBSERVABLE_FEATURE_NODES:
                if feat not in df.columns:
                    df[feat] = 0.0
            df = df.fillna(0.0)

            engine = CausalEngine(
                dag=build_causal_dag(),
                estimation_method=method,
                db_path=settings.db_path,
                model_version=model_version,
                refutation_runs=refutation_runs,
                min_sample_size=min_sample,
            )
            engine.fit(df)
            _causal_engine = engine
            return _causal_engine

        except Exception as exc:
            logger.error("Failed to fit CausalEngine: %s", exc)
            return None


@app.get("/scores/{wallet}/causal-explanation", response_model=CausalExplanationResponse)
def causal_explanation(
    request: Request,
    wallet: str,
    feature_override: Optional[str] = Query(
        default=None,
        description=(
            "Optional feature override in 'feature=value' format. "
            "Returns counterfactual_score with the override applied. "
            "Example: 'wash_ring_membership=0.0'"
        ),
    ),
) -> CausalExplanationResponse:
    """Return a causal explanation of the risk score for ``wallet``.

    Unlike SHAP, which conflates causal and correlational contributions, this
    endpoint returns *causal* average treatment effects (ATEs) estimated via
    do-calculus interventions on the fitted structural causal model.

    Response fields
    ---------------
    - ``feature_ate_table``: ATE of each feature on risk_score — the expected
      change in score if that feature alone is moved from 0 to 1.
    - ``top_causal_features``: top-3 features by absolute ATE.
    - ``counterfactual_score``: predicted score if ``feature_override`` were
      applied (only present when ``feature_override`` is supplied).
    - ``coverage_note``: advisory note about sample size and estimate quality.

    Security
    --------
    - ``feature_override`` is strictly validated: feature must be a known
      observable feature name; value must be a finite float in [-1000, 1000].
    - Rate-limited to 10 requests per minute per IP.
    - ``counterfactual_score`` is not cached publicly to prevent fingerprinting
      the model's sensitivity surface.

    Errors
    ------
    - **400** — invalid Stellar wallet address format.
    - **404** — no scores found for the wallet.
    - **422** — invalid ``feature_override`` format or value.
    - **429** — rate limit exceeded (10 req/min per IP).
    - **503** — causal model not available (insufficient training data, DoWhy
      not installed, or refutation gate triggered).
    """
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    _check_causal_rate_limit(client_ip)

    validate_stellar_address(wallet)

    # Validate feature_override parameter before any expensive work
    override_parsed = _parse_feature_override(feature_override)

    # Fetch current score
    scores = get_latest_scores(wallet=wallet)
    if not scores:
        raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")
    current_score = scores[0].score

    # Get or fit the causal engine
    engine = _get_or_fit_causal_engine()
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Causal model is not available. Either the database has fewer than "
                "CAUSAL_MIN_SAMPLE_SIZE scored wallets, or DoWhy is not installed "
                "(pip install dowhy==0.11.1), or the refutation gate was triggered. "
                "Check server logs for details."
            ),
        )

    # Fetch the ATE table (uses cache when available)
    ate_table = engine.feature_ate_table(use_cache=True)

    # Refutation gate: if the model appears misspecified, refuse to serve ATEs
    try:
        refutation_results = engine.refutation_tests()
        failing = sum(
            1 for k, pval in refutation_results.items()
            if k == "placebo_treatment_refuter" and pval < 0.05
        )
        # A stricter check: if any refutation test fails for more than
        # _MAX_FAILING_REFUTATIONS features, refuse
        all_failing = sum(1 for pval in refutation_results.values() if pval < 0.05)
        if all_failing > _MAX_FAILING_REFUTATIONS:
            logger.error(
                "Causal model refutation gate triggered: %d tests have p < 0.05 "
                "(threshold: %d). Refusing to serve ATE table.",
                all_failing,
                _MAX_FAILING_REFUTATIONS,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Causal model appears misspecified: {all_failing} refutation tests "
                    f"returned p < 0.05 (threshold: {_MAX_FAILING_REFUTATIONS}). "
                    "The causal graph may not fit the current data distribution. "
                    "Please retrain or investigate model specification."
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Refutation failures are logged but don't block the response
        logger.warning("Refutation tests raised an exception: %s", exc)

    # Top-3 features by absolute ATE
    sorted_features = sorted(ate_table.items(), key=lambda x: abs(x[1]), reverse=True)
    top_causal_features = sorted_features[:3]

    # Counterfactual score
    counterfactual_score_value: Optional[float] = None
    if override_parsed is not None:
        feature_name, feature_value = override_parsed
        wallet_features = get_feature_vector_for_wallet(wallet, scores[0].asset_pair)
        if wallet_features is not None:
            counterfactual_score_value = engine.counterfactual_score(
                wallet_features=wallet_features,
                overrides={feature_name: feature_value},
            )
        else:
            # Fall back to score-based approximation
            counterfactual_score_value = engine.counterfactual_score(
                wallet_features={"risk_score": float(current_score)},
                overrides={feature_name: feature_value},
            )

    # Determine sample size for coverage note
    try:
        from detection.storage import _connect
        with _connect() as conn:
            n_wallets = conn.execute("SELECT COUNT(*) FROM risk_scores").fetchone()[0]
    except Exception:
        n_wallets = 0

    coverage_note = (
        f"Based on {n_wallets} scored wallets; causal estimates may be noisy "
        "when sample size is below 500 or when the wallet's feature profile is "
        "unusual relative to the training distribution."
    )

    return CausalExplanationResponse(
        wallet=wallet,
        current_score=current_score,
        feature_ate_table=ate_table,
        top_causal_features=top_causal_features,
        counterfactual_score=counterfactual_score_value,
        coverage_note=coverage_note,
    )
# Mount versioned router and register legacy 302 redirect aliases
# ---------------------------------------------------------------------------

app.include_router(v1_router)

# Legacy bare paths → /v1/... 302 redirects for 90-day deprecation window.
# The DeprecationMiddleware above adds Deprecation/Sunset headers to these responses.
_LEGACY_REDIRECTS = [
    "/health",
    "/scores",
    "/alerts",
    "/assets/risk-ranking",
    "/rings",
    "/correlations",
    "/path-payments/circular",
    "/webhooks",
    "/webhooks/dead-letters",
    "/governance/proposals",
]

for _path in _LEGACY_REDIRECTS:
    # Capture _path in default arg to avoid late-binding closure issue
    def _make_redirect(p):
        def _redirect(request: Request):
            # Preserve query string
            qs = request.url.query
            target = f"/v1{p}" + (f"?{qs}" if qs else "")
            return RedirectResponse(url=target, status_code=302)
        _redirect.__name__ = f"legacy_redirect_{p.replace('/', '_').strip('_')}"
        return _redirect

    app.get(_path, include_in_schema=False)(_make_redirect(_path))


# Parameterised legacy redirects
@app.get("/scores/{wallet}", include_in_schema=False)
def legacy_scores_wallet(wallet: str, request: Request):
    qs = request.url.query
    target = f"/v1/scores/{wallet}" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/scores/{wallet}/explain", include_in_schema=False)
def legacy_scores_explain(wallet: str, request: Request):
    qs = request.url.query
    target = f"/v1/scores/{wallet}/explain" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/scores/{wallet}/counterfactual", include_in_schema=False)
def legacy_scores_counterfactual(wallet: str, request: Request):
    qs = request.url.query
    target = f"/v1/scores/{wallet}/counterfactual" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/wallets/{wallet}/cross-chain", include_in_schema=False)
def legacy_wallets_cross_chain(wallet: str, request: Request):
    qs = request.url.query
    target = f"/v1/wallets/{wallet}/cross-chain" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/amm/pools/{pool_id}/risk", include_in_schema=False)
def legacy_amm_pool_risk(pool_id: str, request: Request):
    qs = request.url.query
    target = f"/v1/amm/pools/{pool_id}/risk" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.post("/feedback", include_in_schema=False)
def legacy_feedback(request: Request):
    return RedirectResponse(url="/v1/feedback", status_code=302)


@app.post("/webhooks", include_in_schema=False)
def legacy_webhooks_post(request: Request):
    return RedirectResponse(url="/v1/webhooks", status_code=302)


@app.delete("/webhooks/{subscriber_id}", include_in_schema=False)
def legacy_delete_webhook(subscriber_id: str, request: Request):
    return RedirectResponse(url=f"/v1/webhooks/{subscriber_id}", status_code=302)


@app.get("/admin/drift-reports", include_in_schema=False)
def legacy_admin_drift_reports(request: Request):
    qs = request.url.query
    target = "/v1/admin/drift-reports" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/admin/robustness-report", include_in_schema=False)
def legacy_admin_robustness_report(request: Request):
    return RedirectResponse(url="/v1/admin/robustness-report", status_code=302)


@app.get("/admin/retrain-runs", include_in_schema=False)
def legacy_admin_retrain_runs(request: Request):
    qs = request.url.query
    target = "/v1/admin/retrain-runs" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.get("/admin/federated/audit-log", include_in_schema=False)
def legacy_admin_federated_audit_log(request: Request):
    qs = request.url.query
    target = "/v1/admin/federated/audit-log" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=302)


@app.post("/disputes", include_in_schema=False)
def legacy_disputes_post(request: Request):
    return RedirectResponse(url="/v1/disputes", status_code=302)


@app.get("/disputes/{dispute_id}", include_in_schema=False)
def legacy_dispute_get(dispute_id: str, request: Request):
    return RedirectResponse(url=f"/v1/disputes/{dispute_id}", status_code=302)


@app.post("/disputes/{dispute_id}/vote", include_in_schema=False)
def legacy_dispute_vote(dispute_id: str, request: Request):
    return RedirectResponse(url=f"/v1/disputes/{dispute_id}/vote", status_code=302)


@app.post("/governance/proposals", include_in_schema=False)
def legacy_governance_proposals_post(request: Request):
    return RedirectResponse(url="/v1/governance/proposals", status_code=302)


@app.post("/governance/proposals/{proposal_id}/vote", include_in_schema=False)
def legacy_governance_proposal_vote(proposal_id: str, request: Request):
    return RedirectResponse(url=f"/v1/governance/proposals/{proposal_id}/vote", status_code=302)
