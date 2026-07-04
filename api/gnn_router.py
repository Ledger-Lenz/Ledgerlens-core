"""FastAPI router for GNN ring-score endpoints.

Endpoints
---------
GET /gnn/ring-score/{wallet}
    Returns the GNN ring membership score and top-5 neighbouring wallets
    by embedding cosine similarity.  The raw embedding vector is never
    returned to prevent adversarial reconstruction of the feature space.
"""
from __future__ import annotations

import logging
import re
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("ledgerlens.api.gnn")

router = APIRouter(prefix="/gnn", tags=["GNN Ring Detection"])

_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{55}$")


def _validate_wallet(wallet: str) -> None:
    if not _STELLAR_ADDRESS_PATTERN.match(wallet):
        raise HTTPException(status_code=400, detail="Invalid Stellar wallet address format.")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class GNNRingScoreResponse(BaseModel):
    """Response for GET /gnn/ring-score/{wallet}."""

    wallet: str
    ring_membership_score: float
    """GNN ring membership probability ∈ [0, 1]. Values above GNN_RING_SCORE_THRESHOLD
    indicate high ring-membership likelihood."""
    top_neighbours: List[str]
    """Up to 5 wallet addresses with highest cosine similarity to the queried
    wallet's embedding.  Raw embedding vectors are not exposed."""
    model_fitted: bool
    """Whether the GNN model is loaded.  False means score is from SCC fallback."""
    fallback_used: bool
    """True when the score was computed via SCC fallback (model not fitted)."""


class GNNHealthResponse(BaseModel):
    model_fitted: bool
    model_path: str
    fallback_to_scc: bool


# ---------------------------------------------------------------------------
# Lazy detector singleton
# ---------------------------------------------------------------------------

_detector = None


def _get_detector():
    """Return a shared GNNRingDetector instance, loaded lazily."""
    global _detector
    if _detector is None:
        try:
            from config.settings import settings
            from detection.gnn_ring_detector import GNNRingDetector

            model_path = getattr(settings, "gnn_model_path", "models/gnn_ring_detector.pt")
            fallback = getattr(settings, "gnn_fallback_to_scc", True)
            _detector = GNNRingDetector(model_path=model_path, fallback_to_scc=fallback)
            _detector.load()
        except Exception as exc:
            logger.warning("Could not initialise GNNRingDetector: %s", exc)
            from detection.gnn_ring_detector import GNNRingDetector

            _detector = GNNRingDetector()  # defaults; will use SCC fallback
    return _detector


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/ring-score/{wallet}",
    response_model=GNNRingScoreResponse,
    summary="Get GNN ring membership score for a wallet",
    description=(
        "Returns the GNN-computed ring membership probability [0,1] for the "
        "given Stellar wallet address, plus the top-5 most similar neighbouring "
        "wallets by embedding cosine similarity.  When the GNN model is not "
        "loaded, the score falls back to the binary SCC membership value."
    ),
)
async def get_gnn_ring_score(
    wallet: str,
    k: int = Query(5, ge=1, le=20, description="Number of top neighbours to return."),
) -> GNNRingScoreResponse:
    """Return GNN ring membership score and top-k similar wallet neighbours."""
    _validate_wallet(wallet)

    detector = _get_detector()
    fallback_used = not detector._fitted

    try:
        # Build a minimal graph from storage if available
        graph = _build_graph_for_wallet(wallet)
        score = detector.predict(wallet, graph)
        neighbours = detector.top_neighbours(wallet, graph, k=k)
    except Exception as exc:
        logger.warning("GNN scoring error for %s: %s", wallet[:8], exc)
        score = 0.0
        neighbours = []
        fallback_used = True

    return GNNRingScoreResponse(
        wallet=wallet,
        ring_membership_score=round(score, 6),
        top_neighbours=neighbours,
        model_fitted=detector._fitted,
        fallback_used=fallback_used,
    )


@router.get(
    "/health",
    response_model=GNNHealthResponse,
    summary="GNN detector health status",
)
async def gnn_health() -> GNNHealthResponse:
    """Return GNN detector status — whether model is loaded and its config."""
    detector = _get_detector()
    return GNNHealthResponse(
        model_fitted=detector._fitted,
        model_path=detector.model_path,
        fallback_to_scc=detector.fallback_to_scc,
    )


# ---------------------------------------------------------------------------
# Internal: graph construction from stored trade data
# ---------------------------------------------------------------------------


def _build_graph_for_wallet(wallet: str):
    """Construct a HeteroData graph from recent trades involving wallet.

    Pulls the last 500 trades for the wallet from the local store.
    Returns an empty-graph stub when PyG is unavailable or no trades found.
    """
    try:
        from detection.gnn_ring_detector import _HAS_PYG, build_transaction_graph

        if not _HAS_PYG:
            return _empty_graph_stub(wallet)

        from config.settings import settings

        db_path = getattr(settings, "ledgerlens_db_path", "./ledgerlens.db")
        trades = _load_recent_trades(db_path, wallet, limit=500)

        if not trades:
            return _empty_graph_stub(wallet)

        def node_feature_fn(w: str):
            import torch

            h = abs(hash(w)) % 10000
            return torch.tensor(
                [h / 10000.0, len(w) / 60.0, float(w.startswith("G")), 0.0],
                dtype=torch.float,
            )

        return build_transaction_graph(trades, node_feature_fn)
    except Exception as exc:
        logger.debug("Graph construction failed: %s", exc)
        return _empty_graph_stub(wallet)


def _empty_graph_stub(wallet: str):
    """Return a minimal stub graph for a single wallet with no edges."""
    try:
        from detection.gnn_ring_detector import HeteroData, _HAS_PYG

        if not _HAS_PYG:
            return None
        import torch

        data = HeteroData()
        data["wallet"].x = torch.zeros((1, 4), dtype=torch.float)
        data["wallet"].wallet_list = [wallet]
        data["wallet", "trades", "wallet"].edge_index = torch.zeros((2, 0), dtype=torch.long)
        data["wallet", "trades", "wallet"].edge_attr = torch.zeros((0, 3))
        return data
    except Exception:
        return None


def _load_recent_trades(db_path: str, wallet: str, limit: int = 500) -> list:
    """Load recent trades from SQLite, returning Trade-like namespace objects."""
    import sqlite3
    from types import SimpleNamespace

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT base_account, counter_account, base_amount,
                   base_asset_code, counter_asset_code, ledger_close_time
            FROM trades
            WHERE base_account = ? OR counter_account = ?
            ORDER BY ledger_close_time DESC
            LIMIT ?
            """,
            (wallet, wallet, limit),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []

    trades = []
    for row in rows:
        try:
            import datetime

            ts_str = row["ledger_close_time"]
            ts = datetime.datetime.fromisoformat(ts_str).timestamp() if ts_str else 0.0
            trades.append(
                SimpleNamespace(
                    base_account=row["base_account"],
                    counter_account=row["counter_account"],
                    base_amount=float(row["base_amount"] or 0),
                    ledger_close_time_ts=ts,
                    base_asset_code=row["base_asset_code"] or "XLM",
                    counter_asset_code=row["counter_asset_code"] or "XLM",
                )
            )
        except Exception:
            continue
    return trades
