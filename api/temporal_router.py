"""
Temporal Pattern Analysis API Endpoints  (Issue #298)
=====================================================
Exposes the temporal anomaly detection engine over HTTP.

Endpoints
---------
GET /temporal/analysis/{wallet}
    Returns all component temporal scores plus a base64-encoded PNG plot of
    the 48-step (4-hour) trade sequence.

Notes
-----
* Requires admin API key.
* Wallets with fewer than TEMPORAL_MIN_TRADES_FOR_ANALYSIS trades receive
  all scores = 0.0 (sparse time series → unreliable results).
* The LSTM model is loaded lazily and gracefully falls back to ARIMA-only
  if the checkpoint is absent or fails checksum verification.
"""
from __future__ import annotations

import base64
import logging
import re
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings

logger = logging.getLogger("ledgerlens.api.temporal")

router = APIRouter(prefix="/temporal", tags=["Temporal Pattern Analysis"])

_STELLAR_ADDRESS_PATTERN = re.compile(r"^G[A-Z2-7]{55}$")

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_lstm_model = None
_lstm_loaded = False


def _get_lstm_model():
    global _lstm_model, _lstm_loaded
    if not _lstm_loaded:
        from detection.temporal_patterns import load_lstm_autoencoder

        model_path = getattr(settings, "temporal_lstm_model_path", "models/lstm_autoencoder.pt")
        _lstm_model = load_lstm_autoencoder(model_path)
        _lstm_loaded = True
    return _lstm_model


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TemporalAnalysisResponse(BaseModel):
    wallet: str
    temporal_anomaly_score: float
    arima_residual_score: float
    lstm_reconstruction_loss: float
    iat_variance_score: float
    burst_synchrony_score: float
    iat_cv: float
    n_trades: int
    sequence_plot_b64: Optional[str] = None  # base64-encoded PNG, or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_wallet_trades(wallet: str, days: int = 7) -> list:
    """Load recent trades for ``wallet`` from SQLite storage."""
    try:
        import sqlite3
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(settings.ledgerlens_db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT base_account, counter_account, base_amount,
                   base_asset_code, counter_asset_code, ledger_close_time
            FROM trades
            WHERE (base_account = ? OR counter_account = ?)
              AND ledger_close_time >= ?
            ORDER BY ledger_close_time ASC
            LIMIT 2000
            """,
            (wallet, wallet, cutoff),
        )
        rows = cur.fetchall()
        conn.close()

        class _TradeLike:
            def __init__(self, row):
                self.base_account = row["base_account"]
                self.counter_account = row["counter_account"]
                try:
                    self.base_amount = float(row["base_amount"] or 0)
                except Exception:
                    self.base_amount = 0.0
                self.base_asset_code = row["base_asset_code"]
                self.counter_asset_code = row["counter_asset_code"]
                ts = row["ledger_close_time"]
                if isinstance(ts, str):
                    try:
                        self.ledger_close_time = datetime.fromisoformat(ts)
                    except Exception:
                        self.ledger_close_time = datetime.now(timezone.utc)
                elif isinstance(ts, (int, float)):
                    self.ledger_close_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    self.ledger_close_time = datetime.now(timezone.utc)

        return [_TradeLike(r) for r in rows]
    except Exception as exc:
        logger.debug("Could not load trades for %s: %s", wallet[:8], exc)
        return []


def _generate_sequence_plot_b64(ts) -> Optional[str]:
    """Generate a 48-step sequence plot and return as base64 PNG."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
        n = len(ts.log_amount_series)
        x = list(range(n))

        ax1.fill_between(x, ts.log_amount_series, alpha=0.6, color="steelblue")
        ax1.set_ylabel("log₁₀(amount)")
        ax1.set_title("Trade amount and count over time (5-min bins)")

        ax2.bar(x, ts.trade_count_series, color="coral", alpha=0.7)
        ax2.set_ylabel("Trade count")
        ax2.set_xlabel("5-minute bin")

        plt.tight_layout()
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=80)
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
    except Exception as exc:
        logger.debug("Could not generate sequence plot: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/analysis/{wallet}",
    response_model=TemporalAnalysisResponse,
    summary="Get temporal pattern analysis for a wallet",
    description=(
        "Returns ARIMA residual, LSTM reconstruction loss, IAT variance, and "
        "burst synchrony component scores, plus the composite temporal_anomaly_score. "
        "Includes a base64-encoded PNG of the 48-step trade sequence. "
        "Wallets with too few trades return all scores as 0.0."
    ),
)
async def get_temporal_analysis(
    wallet: str,
    _: str = Depends(require_admin_key),
) -> TemporalAnalysisResponse:
    if not _STELLAR_ADDRESS_PATTERN.match(wallet):
        raise HTTPException(
            status_code=400,
            detail="Invalid Stellar wallet address format.",
        )

    min_trades = getattr(settings, "temporal_min_trades_for_analysis", 10)
    enabled = getattr(settings, "temporal_enabled", True)

    if not enabled:
        raise HTTPException(
            status_code=503,
            detail="Temporal analysis is disabled (TEMPORAL_ENABLED=false).",
        )

    trades = _load_wallet_trades(wallet)

    if len(trades) < min_trades:
        return TemporalAnalysisResponse(
            wallet=wallet,
            temporal_anomaly_score=0.0,
            arima_residual_score=0.0,
            lstm_reconstruction_loss=0.0,
            iat_variance_score=0.0,
            burst_synchrony_score=0.0,
            iat_cv=1.0,
            n_trades=len(trades),
            sequence_plot_b64=None,
        )

    from detection.temporal_patterns import (
        TradeTimeSeries,
        score_wallet_temporal,
    )

    lstm_model = _get_lstm_model()
    scores = score_wallet_temporal(
        wallet=wallet,
        trades=trades,
        lstm_model=lstm_model,
        min_trades=min_trades,
    )

    # Build time series for the plot
    ts = TradeTimeSeries.from_trades(wallet, trades)
    plot_b64 = _generate_sequence_plot_b64(ts)

    return TemporalAnalysisResponse(
        wallet=wallet,
        temporal_anomaly_score=round(scores["temporal_anomaly_score"], 6),
        arima_residual_score=round(scores["arima_residual_score"], 6),
        lstm_reconstruction_loss=round(scores["lstm_reconstruction_loss"], 6),
        iat_variance_score=round(scores["iat_variance_score"], 6),
        burst_synchrony_score=round(scores["burst_synchrony_score"], 6),
        iat_cv=round(scores["iat_cv"], 6),
        n_trades=scores["n_trades"],
        sequence_plot_b64=plot_b64,
    )
