"""
Temporal Pattern Analysis Engine  (Issue #298)
===============================================
Detects bot-like temporal patterns in wallet trade sequences that are invisible
to aggregate features.  Three complementary approaches are combined into a
single ``temporal_anomaly_score ∈ [0, 1]``:

1. **ARIMA residual detector** — fits ARIMA(2,1,2) on a 7-day rolling window
   of log-trade-amounts binned at 5-minute resolution; scores the latest 24 h
   window as mean absolute standardised residual.
2. **LSTM autoencoder** — reconstructs 48-step (4-hour) sequences; returns
   normalised MSE reconstruction loss.
3. **Inter-arrival time (IAT) variance score** — coefficient of variation of
   inter-trade arrival times.  Metronomic bots have near-zero CV.
4. **Burst synchrony detector** — mean pairwise Pearson correlation of
   trade-count series across suspected ring members.

Attack patterns detected
------------------------
* Metronomic periodicity: fixed-interval bot trades → IAT CV ≈ 0.
* Coordinated burst/idle cycles: high synchrony across ring members.
* Seasonal amplitude masking: suspicious ARIMA residual pattern.

Security / robustness
---------------------
* All ARIMA calls are wrapped in try/except; convergence failures return 0.0.
* Wallets with fewer than ``TEMPORAL_MIN_TRADES_FOR_ANALYSIS`` trades receive
  ``temporal_anomaly_score = 0.0`` (sparse ARIMA fits are unreliable).
* LSTM model integrity is verified via SHA-256 checksum before loading; corrupt
  files fall back to ARIMA-only scoring.
* ``BurstSynchronyDetector.synchrony_score`` should only be called with wallets
  already flagged as potential ring members to avoid O(n²) DoS.

New FEATURE_NAMES added (to be appended after GNN features)
------------------------------------------------------------
* ``temporal_anomaly_score``
* ``iat_variance_score``
* ``burst_synchrony_score``
"""
from __future__ import annotations

import hashlib
import logging
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("ledgerlens.temporal_patterns")

# ---------------------------------------------------------------------------
# Optional PyTorch import
# ---------------------------------------------------------------------------

try:
    import torch
    import torch.nn as nn

    _HAS_TORCH = True
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    _HAS_TORCH = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BIN_MINUTES = 5
SEQUENCE_LENGTH = 48  # 4 hours at 5-min bins
_DEFAULT_WINDOW_HOURS = 168  # 7 days


# ---------------------------------------------------------------------------
# TradeTimeSeries
# ---------------------------------------------------------------------------


@dataclass
class TradeTimeSeries:
    """Wallet trade history binned into fixed-width time intervals.

    Attributes
    ----------
    wallet:
        Stellar wallet address.
    start_ts:
        Inclusive start of the analysis window (UTC).
    end_ts:
        Inclusive end of the analysis window (UTC).
    log_amount_series:
        Shape ``(n_bins,)`` — log10(sum of base_amount) per bin; 0 for empty bins.
    trade_count_series:
        Shape ``(n_bins,)`` — number of trades per bin.
    iat_series:
        Inter-arrival times between consecutive trades in seconds.
        Empty bins do not contribute an IAT; NaN for windows with < 2 trades.
    """

    wallet: str
    start_ts: datetime
    end_ts: datetime
    log_amount_series: np.ndarray
    trade_count_series: np.ndarray
    iat_series: np.ndarray

    @classmethod
    def from_trades(
        cls,
        wallet: str,
        trades: list,
        window_hours: int = _DEFAULT_WINDOW_HOURS,
    ) -> "TradeTimeSeries":
        """Build a TradeTimeSeries for the last ``window_hours`` of trades.

        Parameters
        ----------
        wallet:
            Stellar wallet address.
        trades:
            List of trade objects with ``ledger_close_time`` (datetime or epoch
            float) and ``base_amount`` (numeric).
        window_hours:
            How many hours back from the most recent trade to include.
            Defaults to 168 h (7 days).

        Notes
        -----
        * Bins are ``BIN_MINUTES``-minute (default 5-min) intervals.
        * Empty bins are filled with 0 for ``log_amount`` and ``trade_count``.
        * ``iat_series`` is computed from raw trade timestamps (not bins); NaN
          when fewer than 2 trades are present in the window.
        """
        # --- resolve timestamps ------------------------------------------------
        trade_times: list[float] = []
        trade_amounts: list[float] = []
        for t in trades:
            try:
                ts = t.ledger_close_time
                if isinstance(ts, datetime):
                    epoch = ts.timestamp()
                else:
                    epoch = float(ts)
                trade_times.append(epoch)
                try:
                    trade_amounts.append(float(t.base_amount or 0))
                except Exception:
                    trade_amounts.append(0.0)
            except Exception:
                continue

        if not trade_times:
            n_bins = int(window_hours * 60 / BIN_MINUTES)
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=window_hours)
            return cls(
                wallet=wallet,
                start_ts=start,
                end_ts=now,
                log_amount_series=np.zeros(n_bins, dtype=np.float32),
                trade_count_series=np.zeros(n_bins, dtype=np.float32),
                iat_series=np.array([], dtype=np.float32),
            )

        # --- window bounds -----------------------------------------------------
        t_max = max(trade_times)
        t_min = t_max - window_hours * 3600.0
        filtered = [
            (t, a)
            for t, a in zip(trade_times, trade_amounts)
            if t >= t_min
        ]
        if filtered:
            sorted_pairs = sorted(filtered, key=lambda x: x[0])
            f_times = [p[0] for p in sorted_pairs]
            f_amounts = [p[1] for p in sorted_pairs]
        else:
            f_times, f_amounts = [], []

        # --- bin construction --------------------------------------------------
        bin_secs = BIN_MINUTES * 60
        n_bins = int(window_hours * 60 / BIN_MINUTES)
        log_amounts = np.zeros(n_bins, dtype=np.float64)
        counts = np.zeros(n_bins, dtype=np.float64)

        for ts, amt in zip(f_times, f_amounts):
            bin_idx = int((ts - t_min) / bin_secs)
            if 0 <= bin_idx < n_bins:
                log_amounts[bin_idx] += max(amt, 0.0)
                counts[bin_idx] += 1.0

        # Convert summed amounts to log10
        log_amounts = np.where(log_amounts > 0, np.log10(log_amounts + 1e-9), 0.0)

        # --- inter-arrival times -----------------------------------------------
        if len(f_times) >= 2:
            iat = np.diff(sorted(f_times), n=1).astype(np.float32)
        else:
            iat = np.array([], dtype=np.float32)

        start_dt = datetime.fromtimestamp(t_min, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(t_max, tz=timezone.utc)

        return cls(
            wallet=wallet,
            start_ts=start_dt,
            end_ts=end_dt,
            log_amount_series=log_amounts.astype(np.float32),
            trade_count_series=counts.astype(np.float32),
            iat_series=iat,
        )

    @property
    def iat_cv(self) -> float:
        """Coefficient of variation of inter-arrival times.

        Low CV → metronomic bot behaviour.

        Returns 1.0 when fewer than 2 trades are present (insufficient data —
        treated as normal/non-suspicious).
        """
        valid = self.iat_series[~np.isnan(self.iat_series)]
        if len(valid) < 2:
            return 1.0
        mean = float(np.mean(valid))
        std = float(np.std(valid))
        return float(std / (mean + 1e-9))


# ---------------------------------------------------------------------------
# ARIMAResidualDetector
# ---------------------------------------------------------------------------


class ARIMAResidualDetector:
    """Fits ARIMA(2,1,2) on a rolling training window and scores residuals.

    Parameters
    ----------
    fit_window_days:
        Number of days of history to use as the training window.
    score_window_steps:
        Number of bins at the end of the series to score as the test window.
        Defaults to 288 (24 h at 5-min bins).
    """

    ORDER = (2, 1, 2)

    def __init__(
        self,
        fit_window_days: int = 7,
        score_window_steps: int = 288,
    ) -> None:
        self._fit_window_days = fit_window_days
        self._score_window_steps = score_window_steps
        self._result = None
        self._residual_std: float = 0.0

    def fit(self, series: np.ndarray) -> None:
        """Fit ARIMA(2,1,2) on ``series[:-score_window_steps]``.

        Convergence warnings are suppressed.  Non-convergence silently sets
        ``_result = None`` so ``score()`` returns 0.0 safely.
        """
        try:
            from statsmodels.tsa.arima.model import ARIMA as _ARIMA
        except ImportError:
            logger.warning("statsmodels not installed; ARIMA scoring unavailable.")
            return

        n = len(series)
        if n <= self._score_window_steps + 3:
            # Insufficient data for both training and test windows
            return

        train = series[: n - self._score_window_steps]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = _ARIMA(train, order=self.ORDER)
                self._result = model.fit()
                self._residual_std = float(np.std(self._result.resid))
        except Exception as exc:
            logger.debug("ARIMA fit failed (non-fatal): %s", exc)
            self._result = None
            self._residual_std = 0.0

    def score(self, series: np.ndarray) -> float:
        """Return mean absolute standardised residual for the test window ∈ [0, 1].

        Returns 0.0 when the model has not been fitted or residual std ≈ 0.
        """
        if self._result is None or self._residual_std < 1e-6:
            return 0.0
        try:
            forecast = self._result.forecast(steps=self._score_window_steps)
            actual = series[-self._score_window_steps :]
            n = min(len(forecast), len(actual))
            residuals = np.abs(actual[:n] - forecast[:n]) / (self._residual_std + 1e-9)
            raw = float(np.mean(residuals)) / 5.0  # empirical normalisation
            return float(np.clip(raw, 0.0, 1.0))
        except Exception as exc:
            logger.debug("ARIMA score failed (non-fatal): %s", exc)
            return 0.0


# ---------------------------------------------------------------------------
# LSTM autoencoder
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class LSTMAutoencoder(nn.Module):
        """Sequence-to-sequence LSTM autoencoder for 5-minute trade bins.

        Input shape: ``(batch, seq_len, input_dim)``
        where ``input_dim = 2`` (log_amount, trade_count per step).

        The encoder produces a fixed-size context vector; the decoder
        reconstructs the sequence by repeating the last hidden state.

        Parameters
        ----------
        input_dim:
            Number of features per time step (default 2).
        hidden_dim:
            LSTM hidden size (default 64).
        num_layers:
            Number of stacked LSTM layers (default 2).
        sequence_length:
            Expected sequence length (default 48 = 4 h at 5-min bins).
        dropout:
            Dropout applied between stacked LSTM layers (default 0.2).
        """

        def __init__(
            self,
            input_dim: int = 2,
            hidden_dim: int = 64,
            num_layers: int = 2,
            sequence_length: int = SEQUENCE_LENGTH,
            dropout: float = 0.2,
        ) -> None:
            super().__init__()
            lstm_drop = dropout if num_layers > 1 else 0.0
            self.encoder = nn.LSTM(
                input_dim,
                hidden_dim,
                num_layers,
                batch_first=True,
                dropout=lstm_drop,
            )
            self.decoder = nn.LSTM(
                hidden_dim,
                hidden_dim,
                num_layers,
                batch_first=True,
                dropout=lstm_drop,
            )
            self.output_layer = nn.Linear(hidden_dim, input_dim)
            self.sequence_length = sequence_length

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            """Encode then decode; returns reconstruction shape matching ``x``."""
            _, (h, c) = self.encoder(x)
            # Tile the last hidden state across the sequence for decoding
            decoder_input = h[-1].unsqueeze(1).repeat(1, self.sequence_length, 1)
            decoded, _ = self.decoder(decoder_input, (h, c))
            return self.output_layer(decoded)

        def reconstruction_loss(self, x: "torch.Tensor") -> float:
            """MSE between ``x`` and its reconstruction, normalised to [0, 1].

            Uses ``min(mse / 10.0, 1.0)`` as empirical normalisation.
            """
            with torch.no_grad():
                recon = self(x)
                mse = torch.mean((x - recon) ** 2).item()
            return float(min(mse / 10.0, 1.0))

else:  # pragma: no cover

    class LSTMAutoencoder:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch not installed.")


# ---------------------------------------------------------------------------
# LSTM model loader with checksum verification
# ---------------------------------------------------------------------------


def load_lstm_autoencoder(
    model_path: str,
    input_dim: int = 2,
    hidden_dim: int = 64,
    num_layers: int = 2,
    sequence_length: int = SEQUENCE_LENGTH,
    dropout: float = 0.2,
) -> Optional["LSTMAutoencoder"]:
    """Load a trained LSTMAutoencoder from ``model_path``.

    Verifies the SHA-256 checksum stored at
    ``model_path.replace('.pt', '.sha256')`` before loading.  Returns ``None``
    on failure (triggers ARIMA-only scoring).

    Parameters
    ----------
    model_path:
        Path to the ``.pt`` checkpoint.
    input_dim, hidden_dim, num_layers, sequence_length, dropout:
        Architecture hyperparameters — must match the saved checkpoint.
    """
    if not _HAS_TORCH:
        return None

    if not os.path.exists(model_path):
        logger.info("LSTM autoencoder not found at %s — ARIMA-only scoring.", model_path)
        return None

    checksum_path = model_path.replace(".pt", ".sha256")
    if os.path.exists(checksum_path):
        try:
            stored = Path(checksum_path).read_text().strip().lower()
            h = hashlib.sha256()
            with open(model_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            computed = h.hexdigest()
            if computed != stored:
                logger.error(
                    "LSTM autoencoder checksum MISMATCH for %s — ARIMA-only scoring.",
                    model_path,
                )
                return None
        except Exception as exc:
            logger.error("LSTM checksum verification failed: %s", exc)
            return None

    try:
        checkpoint = torch.load(model_path, weights_only=True, map_location="cpu")
        model = LSTMAutoencoder(
            input_dim=checkpoint.get("input_dim", input_dim),
            hidden_dim=checkpoint.get("hidden_dim", hidden_dim),
            num_layers=checkpoint.get("num_layers", num_layers),
            sequence_length=checkpoint.get("sequence_length", sequence_length),
            dropout=checkpoint.get("dropout", dropout),
        )
        model.load_state_dict(checkpoint["state_dict"])
        model.eval()
        return model
    except Exception as exc:
        logger.error("Failed to load LSTM autoencoder: %s — ARIMA-only scoring.", exc)
        return None


# ---------------------------------------------------------------------------
# BurstSynchronyDetector
# ---------------------------------------------------------------------------


class BurstSynchronyDetector:
    """Detects coordinated burst patterns across wallets in a ring.

    Parameters
    ----------
    bin_minutes:
        Bin width in minutes (must match the trade series resolution).
    """

    def __init__(self, bin_minutes: int = BIN_MINUTES) -> None:
        self._bin_minutes = bin_minutes

    def synchrony_score(
        self,
        series_map: dict[str, np.ndarray],
    ) -> float:
        """Mean pairwise Pearson correlation of trade-count series.

        High positive correlation → wallets trade in coordinated bursts
        (ring behaviour).

        Parameters
        ----------
        series_map:
            Mapping of ``wallet → trade_count_series`` (1-D float array of
            equal length).

        Returns
        -------
        float
            Clipped to [0, 1].  Negative correlations are not suspicious;
            they are treated as 0.  Returns 0.0 when fewer than 2 wallets
            are provided or when all series are zero-valued.
        """
        wallets = list(series_map.keys())
        if len(wallets) < 2:
            return 0.0

        # Trim / pad all series to the same length
        lengths = [len(series_map[w]) for w in wallets]
        min_len = min(lengths)
        if min_len == 0:
            return 0.0

        arrays = [series_map[w][:min_len].astype(np.float64) for w in wallets]

        # Drop wallets with zero-only series (they carry no synchrony signal)
        arrays = [a for a in arrays if np.any(a != 0)]
        if len(arrays) < 2:
            return 0.0

        # Compute mean pairwise Pearson correlation
        n = len(arrays)
        correlations: list[float] = []
        for i in range(n):
            for j in range(i + 1, n):
                a, b = arrays[i], arrays[j]
                std_a, std_b = np.std(a), np.std(b)
                if std_a < 1e-9 or std_b < 1e-9:
                    correlations.append(0.0)
                    continue
                corr = float(np.corrcoef(a, b)[0, 1])
                if np.isnan(corr):
                    corr = 0.0
                correlations.append(max(corr, 0.0))  # clip negatives to 0

        if not correlations:
            return 0.0
        return float(np.clip(np.mean(correlations), 0.0, 1.0))


# ---------------------------------------------------------------------------
# TemporalPatternScorer
# ---------------------------------------------------------------------------


class TemporalPatternScorer:
    """Combines component temporal scores into a single anomaly score.

    The composite score is a weighted sum of four components:

    * ``arima_residual`` — ARIMA forecast residual score ∈ [0, 1].
    * ``lstm_loss`` — LSTM reconstruction loss ∈ [0, 1].
    * ``iat_cv`` — IAT coefficient of variation (low = metronomic); converted
      to ``iat_score = 1 - min(iat_cv, 1.0)``.
    * ``synchrony`` — burst synchrony score ∈ [0, 1].

    Default weights (configurable): 0.3 / 0.3 / 0.2 / 0.2 (sum = 1.0).

    Parameters
    ----------
    arima_weight, lstm_weight, iat_weight, synchrony_weight:
        Component weights (must sum to 1.0 within ε = 1e-5).
    """

    def __init__(
        self,
        arima_weight: float = 0.3,
        lstm_weight: float = 0.3,
        iat_weight: float = 0.2,
        synchrony_weight: float = 0.2,
    ) -> None:
        total = arima_weight + lstm_weight + iat_weight + synchrony_weight
        assert (
            abs(total - 1.0) < 1e-5
        ), f"Temporal weights must sum to 1.0, got {total:.6f}."
        self.arima_weight = arima_weight
        self.lstm_weight = lstm_weight
        self.iat_weight = iat_weight
        self.synchrony_weight = synchrony_weight

    def score(
        self,
        arima_residual: float,
        lstm_loss: float,
        iat_cv: float,
        synchrony: float,
    ) -> float:
        """Compute composite temporal anomaly score ∈ [0, 1].

        Parameters
        ----------
        arima_residual:
            Normalised ARIMA residual score ∈ [0, 1].
        lstm_loss:
            Normalised LSTM reconstruction loss ∈ [0, 1].
        iat_cv:
            IAT coefficient of variation (0 = perfectly metronomic; 1+ = normal).
        synchrony:
            Burst synchrony score ∈ [0, 1].
        """
        iat_score = 1.0 - min(float(iat_cv), 1.0)
        raw = (
            self.arima_weight * float(arima_residual)
            + self.lstm_weight * float(lstm_loss)
            + self.iat_weight * iat_score
            + self.synchrony_weight * float(synchrony)
        )
        return float(np.clip(raw, 0.0, 1.0))


# ---------------------------------------------------------------------------
# High-level convenience: score a single wallet
# ---------------------------------------------------------------------------


def score_wallet_temporal(
    wallet: str,
    trades: list,
    synchrony_series_map: Optional[dict[str, np.ndarray]] = None,
    lstm_model: Optional["LSTMAutoencoder"] = None,
    scorer: Optional[TemporalPatternScorer] = None,
    min_trades: int = 10,
) -> dict:
    """Compute all temporal component scores for a wallet.

    Returns a dict with keys:
    * ``temporal_anomaly_score``  ∈ [0, 1]
    * ``arima_residual_score``    ∈ [0, 1]
    * ``lstm_reconstruction_loss`` ∈ [0, 1]
    * ``iat_variance_score``      ∈ [0, 1]  (= 1 - min(iat_cv, 1.0))
    * ``burst_synchrony_score``   ∈ [0, 1]
    * ``iat_cv``                  (raw)
    * ``n_trades``                (count used)

    Wallets with fewer than ``min_trades`` receive all scores = 0.0.
    """
    result = {
        "temporal_anomaly_score": 0.0,
        "arima_residual_score": 0.0,
        "lstm_reconstruction_loss": 0.0,
        "iat_variance_score": 0.0,
        "burst_synchrony_score": 0.0,
        "iat_cv": 1.0,
        "n_trades": 0,
    }

    if not trades or len(trades) < min_trades:
        return result

    ts = TradeTimeSeries.from_trades(wallet, trades)
    result["n_trades"] = len(trades)
    result["iat_cv"] = ts.iat_cv

    # --- ARIMA ----------------------------------------------------------------
    arima = ARIMAResidualDetector()
    try:
        arima.fit(ts.log_amount_series)
        arima_score = arima.score(ts.log_amount_series)
    except Exception as exc:
        logger.debug("ARIMA scoring failed for %s: %s", wallet[:8], exc)
        arima_score = 0.0
    result["arima_residual_score"] = arima_score

    # --- LSTM -----------------------------------------------------------------
    lstm_score = 0.0
    if lstm_model is not None and _HAS_TORCH:
        try:
            n = SEQUENCE_LENGTH
            log_seq = ts.log_amount_series[-n:] if len(ts.log_amount_series) >= n else np.pad(
                ts.log_amount_series, (n - len(ts.log_amount_series), 0)
            )
            cnt_seq = ts.trade_count_series[-n:] if len(ts.trade_count_series) >= n else np.pad(
                ts.trade_count_series, (n - len(ts.trade_count_series), 0)
            )
            seq = np.stack([log_seq, cnt_seq], axis=1).astype(np.float32)  # (48, 2)
            x = torch.tensor(seq).unsqueeze(0)  # (1, 48, 2)
            lstm_score = lstm_model.reconstruction_loss(x)
        except Exception as exc:
            logger.debug("LSTM scoring failed for %s: %s", wallet[:8], exc)
            lstm_score = 0.0
    result["lstm_reconstruction_loss"] = lstm_score

    # --- Burst synchrony ------------------------------------------------------
    synchrony_score = 0.0
    if synchrony_series_map and len(synchrony_series_map) >= 2:
        try:
            detector = BurstSynchronyDetector()
            synchrony_score = detector.synchrony_score(synchrony_series_map)
        except Exception as exc:
            logger.debug("Synchrony scoring failed: %s", exc)
            synchrony_score = 0.0
    result["burst_synchrony_score"] = synchrony_score

    # --- Composite ------------------------------------------------------------
    if scorer is None:
        scorer = TemporalPatternScorer()
    composite = scorer.score(
        arima_residual=arima_score,
        lstm_loss=lstm_score,
        iat_cv=ts.iat_cv,
        synchrony=synchrony_score,
    )
    result["temporal_anomaly_score"] = composite
    result["iat_variance_score"] = 1.0 - min(ts.iat_cv, 1.0)

    return result
