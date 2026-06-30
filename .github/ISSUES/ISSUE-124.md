---
title: "Build Temporal Pattern Analysis Engine for Time-Series Wash Trading Detection"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

LedgerLens feature extraction captures aggregate statistics (trade volume, Benford deviation, round-trip frequency) over fixed 24-hour windows, collapsing the temporal structure of trade sequences into scalars. A temporal pattern analysis engine that models trade sequences as time series — using ARIMA residual anomaly detection for steady-state deviation and an LSTM autoencoder for sequence-level pattern matching — captures bot-like periodicity, metronomic timing, and coordinated burst patterns that aggregate features cannot express.

## Background & Context

Sophisticated wash trading bots operate with characteristic temporal signatures that are invisible to aggregation:

1. **Metronomic periodicity**: bots execute trades at fixed intervals (e.g., every 47 seconds) to match a target volume. This appears as a near-zero inter-arrival time variance — statistically implausible for human trading.
2. **Coordinated burst patterns**: multiple wallets in a ring trade in rapid bursts separated by long idle periods. The burst–idle ratio and the synchrony across wallets (Pearson correlation of trade timestamps) betray coordination.
3. **Seasonal amplitude masking**: bots adjust trade volume in proportion to the observed market volume to avoid detection by volume-based features. This appears as a suspicious correlation between the bot's volume and the market's volume — the opposite of what genuine traders do (genuine traders react to, not mirror, market activity).

The temporal engine models each wallet's trade sequence as a univariate time series of log-trade-amounts binned at 5-minute resolution, fits an ARIMA(p,d,q) model to the in-distribution wallets, and scores anomalies as the standardised residual. The LSTM autoencoder provides a complementary reconstruction-loss score for sequence-level patterns the ARIMA residual misses.

`detection/temporal_patterns.py` is a stub. This issue is the full implementation.

## Objectives

- [ ] Implement `TradeTimeSeries` that bins trades into 5-minute intervals and computes log-amount, trade-count, and inter-arrival time (IAT) series
- [ ] Implement `ARIMAResidualDetector` that fits `ARIMA(2,1,2)` on a rolling 7-day window and scores the latest 24h window as standardised residuals
- [ ] Implement `LSTMAutoencoder` that reconstructs 48-step (4-hour) trade sequences and returns per-sequence reconstruction loss
- [ ] Implement `BurstSynchronyDetector` that cross-correlates trade timestamp series across ring members to detect coordinated bursts
- [ ] Implement `TemporalPatternScorer` that combines ARIMA residual, LSTM reconstruction loss, IAT variance score, and burst synchrony into a single `temporal_anomaly_score` ∈ [0, 1]
- [ ] Add `temporal_anomaly_score`, `iat_variance_score`, and `burst_synchrony_score` to `FEATURE_NAMES`
- [ ] Expose `GET /temporal/analysis/{wallet}` returning the component scores and a 48-step trade sequence plot as base64-encoded PNG
- [ ] Write tests covering: binning, ARIMA residual computation, LSTM forward pass shape, synchrony detection, and the fallback path when fewer than 48 data points exist

## Technical Requirements

### Trade time series builder

```python
# detection/temporal_patterns.py

import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

BIN_MINUTES = 5
SEQUENCE_LENGTH = 48   # 4 hours at 5-min bins

@dataclass
class TradeTimeSeries:
    wallet: str
    start_ts: datetime
    end_ts: datetime
    log_amount_series: np.ndarray    # shape: (n_bins,) — log10(sum_amount) per bin, 0 if empty
    trade_count_series: np.ndarray   # shape: (n_bins,) — trade count per bin
    iat_series: np.ndarray           # inter-arrival times between consecutive trades (seconds)

    @classmethod
    def from_trades(cls, wallet: str, trades: list, window_hours: int = 168) -> "TradeTimeSeries":
        """
        Build time series for the last `window_hours` hours of trades.
        Bins are `BIN_MINUTES`-minute intervals.
        Empty bins are filled with 0 (amount) or nan (IAT).
        """
        ...

    @property
    def iat_cv(self) -> float:
        """Coefficient of variation of inter-arrival times. Low CV → metronomic bot."""
        valid = self.iat_series[~np.isnan(self.iat_series)]
        if len(valid) < 2:
            return 1.0   # insufficient data → assume normal
        return float(np.std(valid) / (np.mean(valid) + 1e-9))
```

### ARIMA residual detector

```python
from statsmodels.tsa.arima.model import ARIMA
import warnings

class ARIMAResidualDetector:
    ORDER = (2, 1, 2)

    def __init__(self, fit_window_days: int = 7, score_window_steps: int = 288): ...

    def fit(self, series: np.ndarray) -> None:
        """
        Fit ARIMA(2,1,2) on `series[:-score_window_steps]` (training window).
        Suppress statsmodels convergence warnings — non-convergence returns score=0.0.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._model = ARIMA(series[:-self.score_window_steps], order=self.ORDER)
            self._result = self._model.fit()
            self._residual_std = float(np.std(self._result.resid))

    def score(self, series: np.ndarray) -> float:
        """
        Forecast the last `score_window_steps` steps and compute
        mean absolute standardised residual over the forecast window.
        Returns 0.0 if not fitted or residual_std ≈ 0.
        """
        if self._result is None or self._residual_std < 1e-6:
            return 0.0
        forecast = self._result.forecast(steps=self.score_window_steps)
        actual = series[-self.score_window_steps:]
        residuals = np.abs(actual - forecast) / (self._residual_std + 1e-9)
        return float(np.clip(np.mean(residuals) / 5.0, 0.0, 1.0))  # normalise to [0,1]
```

### LSTM autoencoder

```python
import torch
import torch.nn as nn

class LSTMAutoencoder(nn.Module):
    def __init__(
        self,
        input_dim: int = 2,         # [log_amount, trade_count] per step
        hidden_dim: int = 64,
        num_layers: int = 2,
        sequence_length: int = SEQUENCE_LENGTH,
        dropout: float = 0.2,
    ):
        super().__init__()
        self.encoder = nn.LSTM(
            input_dim, hidden_dim, num_layers,
            batch_first=True, dropout=dropout,
        )
        self.decoder = nn.LSTM(
            hidden_dim, hidden_dim, num_layers,
            batch_first=True, dropout=dropout,
        )
        self.output_layer = nn.Linear(hidden_dim, input_dim)
        self.sequence_length = sequence_length

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x shape: (batch, seq_len, input_dim). Returns reconstruction."""
        _, (h, c) = self.encoder(x)
        # Decode by repeating the last hidden state
        decoder_input = h[-1].unsqueeze(1).repeat(1, self.sequence_length, 1)
        decoded, _ = self.decoder(decoder_input, (h, c))
        return self.output_layer(decoded)

    def reconstruction_loss(self, x: torch.Tensor) -> float:
        """Mean squared error between x and its reconstruction. Normalised to [0,1]."""
        with torch.no_grad():
            recon = self(x)
            mse = torch.mean((x - recon) ** 2).item()
        return float(min(mse / 10.0, 1.0))   # empirical normalisation constant
```

### Burst synchrony detector

```python
class BurstSynchronyDetector:
    def __init__(self, bin_minutes: int = BIN_MINUTES): ...

    def synchrony_score(
        self,
        series_map: dict[str, np.ndarray],   # wallet → trade_count_series
    ) -> float:
        """
        Compute mean pairwise Pearson correlation of trade_count_series across all wallets.
        High correlation → coordinated burst pattern.
        Returns 0.0 if fewer than 2 wallets or all series are zero.
        Clips to [0, 1] (negative correlation is not suspicious).
        """
        ...
```

### Composite temporal pattern scorer

```python
class TemporalPatternScorer:
    def __init__(
        self,
        arima_weight: float = 0.3,
        lstm_weight: float = 0.3,
        iat_weight: float = 0.2,
        synchrony_weight: float = 0.2,
    ):
        assert abs(arima_weight + lstm_weight + iat_weight + synchrony_weight - 1.0) < 1e-6

    def score(
        self,
        arima_residual: float,
        lstm_loss: float,
        iat_cv: float,       # coefficient of variation — low = metronomic
        synchrony: float,
    ) -> float:
        """
        iat_score = 1 - min(iat_cv, 1.0)   # low CV → high score
        temporal_anomaly_score = weighted sum of components
        """
        iat_score = 1.0 - min(iat_cv, 1.0)
        return float(
            self.arima_weight * arima_residual
            + self.lstm_weight * lstm_loss
            + self.iat_weight * iat_score
            + self.synchrony_weight * synchrony
        )
```

### Configuration

```
TEMPORAL_BIN_MINUTES=5
TEMPORAL_SEQUENCE_LENGTH=48
TEMPORAL_ARIMA_FIT_WINDOW_DAYS=7
TEMPORAL_LSTM_MODEL_PATH=models/lstm_autoencoder.pt
TEMPORAL_ENABLED=true
TEMPORAL_MIN_TRADES_FOR_ANALYSIS=10   # skip analysis if fewer trades in window
```

## Security Considerations

- **LSTM model integrity**: `models/lstm_autoencoder.pt` must be verified against a SHA-256 checksum (same pattern as ISSUE-121) before loading. Corrupt or tampered model files must trigger graceful degradation to ARIMA-only scoring, not an exception
- **ARIMA convergence failure**: `statsmodels.ARIMA.fit()` can fail to converge on degenerate series (all-zero series, constant series). All ARIMA calls must be wrapped in `try/except` returning `0.0` on failure — the overall `temporal_anomaly_score` must always be computable even if one component is unavailable
- **Minimum data guard**: wallets with fewer than `TEMPORAL_MIN_TRADES_FOR_ANALYSIS` trades in the analysis window must receive `temporal_anomaly_score=0.0` rather than a computed score from a sparse series. Sparse ARIMA fits are unreliable and could produce false positives on low-activity wallets
- **Synchrony computation on ring members only**: `BurstSynchronyDetector.synchrony_score` must only be called with wallets that are already flagged as potential ring members (SCC or GNN score > 0.5). Computing pairwise correlations across all wallets would be O(n²) and a DoS vector

## Testing Requirements

- [ ] `tests/test_temporal_patterns.py`
- [ ] Test: `TradeTimeSeries.from_trades` with 100 evenly spaced trades produces correct bin counts and `iat_cv` near zero
- [ ] Test: `TradeTimeSeries.iat_cv` returns 1.0 when fewer than 2 trades are present
- [ ] Test: `ARIMAResidualDetector.score` returns `0.0` when not fitted
- [ ] Test: `ARIMAResidualDetector.score` returns a value in [0, 1] for a non-degenerate series
- [ ] Test: `LSTMAutoencoder` forward pass produces output shape matching input `(batch, seq_len, input_dim)`
- [ ] Test: `LSTMAutoencoder.reconstruction_loss` returns `0.0` for a constant input (no variance to reconstruct)
- [ ] Test: `BurstSynchronyDetector.synchrony_score` returns `0.0` for a single wallet
- [ ] Test: `TemporalPatternScorer.score` weights sum to 1.0 (constructor assertion)
- [ ] Test: wallet with fewer than `TEMPORAL_MIN_TRADES_FOR_ANALYSIS` trades returns `temporal_anomaly_score=0.0`
- [ ] Integration test: `GET /temporal/analysis/{wallet}` returns correct schema with all component scores

## Documentation Requirements

- [ ] Docstrings on `TradeTimeSeries`, `ARIMAResidualDetector`, `LSTMAutoencoder`, `BurstSynchronyDetector`, `TemporalPatternScorer`
- [ ] `docs/temporal_analysis.md`: motivation (aggregate features miss temporal structure), three attack patterns detected, component score descriptions, ARIMA failure modes and mitigation, minimum data requirements, LSTM training procedure (separate from inference)
- [ ] Training script `scripts/train_lstm_autoencoder.py` with `--epochs`, `--lr`, `--neg-sample-ratio` flags
- [ ] Update `docs/detection_pipeline.md` to include the temporal analysis stage
- [ ] Update `.env.example` with the six new configuration variables

## Definition of Done

- [ ] `TradeTimeSeries`, `ARIMAResidualDetector`, `LSTMAutoencoder`, `BurstSynchronyDetector`, `TemporalPatternScorer` fully implemented
- [ ] `temporal_anomaly_score`, `iat_variance_score`, `burst_synchrony_score` in `FEATURE_NAMES`
- [ ] `GET /temporal/analysis/{wallet}` endpoint live
- [ ] ARIMA and LSTM failure modes return `0.0`, not exceptions, verified by tests
- [ ] All tests pass
- [ ] `docs/temporal_analysis.md` authored
- [ ] `.env.example` updated

## For Contributors

**Ideal contributor profile**: You have experience applying time-series analysis to anomaly detection in production — specifically ARIMA-family models and LSTM autoencoders for sequence reconstruction. Familiarity with the statsmodels ARIMA API and PyTorch LSTM layers is required. Experience with financial high-frequency data, bot detection via temporal features, or wash trading detection at an exchange is a strong advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "time-series anomaly detection", "ARIMA / LSTM for fraud detection", "bot behaviour analysis"
2. **Relevant experience** — time-series anomaly detection systems you have built; ARIMA or LSTM models in production; financial HFT or bot detection work
3. **Approach / initial thoughts** — ARIMA(2,1,2) order selection rationale (would you choose differently?); how you would handle the all-zero bin problem in the LSTM input; alternative temporal features you would add
4. **Estimated time** — breakdown by component (time series builder, ARIMA detector, LSTM autoencoder, synchrony detector, scorer, API endpoint, training script, tests, docs)
