# Temporal Pattern Analysis Engine  (Issue #298)

## Motivation: Aggregate Features Miss Temporal Structure

LedgerLens feature extraction collapses trade sequences into 24-hour aggregate
statistics. Three attack patterns are invisible to this approach:

### 1. Metronomic Periodicity
Wash trading bots execute trades at fixed intervals (e.g., every 47 seconds)
to match a target volume. This appears as near-zero inter-arrival time (IAT)
variance — statistically implausible for human traders who react to market
conditions.

### 2. Coordinated Burst Patterns
Multiple wallets in a ring trade in rapid bursts separated by long idle
periods. The burst–idle ratio and Pearson correlation of trade timestamps
across ring members betrays coordination. Aggregate features see only the
total volume.

### 3. Seasonal Amplitude Masking
Sophisticated bots adjust trade volume in proportion to observed market
volume to evade volume-based detectors. This creates a suspicious positive
correlation between the bot's volume and market volume — genuine traders
react to, not mirror, market activity.

## Components

### TradeTimeSeries (`detection/temporal_patterns.py`)

Bins trades into 5-minute intervals:
- `log_amount_series` — log10(sum of base_amount) per bin
- `trade_count_series` — count per bin  
- `iat_series` — raw inter-arrival times in seconds
- `iat_cv` property — coefficient of variation of IAT (0 ≈ bot, 1+ ≈ human)

### ARIMAResidualDetector

- Fits `ARIMA(2,1,2)` on `series[:-288]` (7-day training window, 24h test window)
- Scores the test window as mean absolute standardised residual, normalised to [0, 1]
- All convergence failures silently return `0.0`

### LSTMAutoencoder

- Sequence-to-sequence LSTM: encoder → context vector → decoder
- Input: 48-step sequences of `(log_amount, trade_count)` per 5-min bin
- Reconstruction loss = MSE / 10.0, clipped to [0, 1]
- Clean wallets reconstruct with low loss; bot-like sequences reconstruct poorly

### BurstSynchronyDetector

- Computes mean pairwise Pearson correlation of trade-count series
- Negative correlations treated as 0 (not suspicious)
- Only called for wallets already flagged by SCC or GNN (score > 0.5) to avoid O(n²) cost

### TemporalPatternScorer

Weighted sum of all components:

| Component | Default weight | Score description |
|---|---|---|
| ARIMA residual | 0.30 | Forecast deviation from expected pattern |
| LSTM loss | 0.30 | Reconstruction difficulty |
| IAT score | 0.20 | `1 - min(iat_cv, 1.0)` (low CV → high score) |
| Burst synchrony | 0.20 | Cross-wallet trade timing correlation |

## New Feature Names

Three features are added to `FEATURE_NAMES`:

| Feature | Range | Description |
|---|---|---|
| `temporal_anomaly_score` | [0, 1] | Composite weighted score |
| `iat_variance_score` | [0, 1] | `1 - min(iat_cv, 1.0)` |
| `burst_synchrony_score` | [0, 1] | Mean pairwise Pearson correlation |

## ARIMA Failure Modes and Mitigations

| Failure | Mitigation |
|---|---|
| Non-convergence | `try/except` around `ARIMA.fit()` → returns `0.0` |
| All-zero series | `residual_std < 1e-6` guard → returns `0.0` |
| Constant series | Same guard covers this case |
| Fewer data points than test window | `n <= score_window_steps + 3` guard |

## Minimum Data Requirement

Wallets with fewer than `TEMPORAL_MIN_TRADES_FOR_ANALYSIS` (default 10) trades
in the analysis window receive `temporal_anomaly_score = 0.0`. Sparse ARIMA
fits on low-activity wallets are unreliable and can produce false positives.

## LSTM Training

```bash
python scripts/train_lstm_autoencoder.py \
  --epochs 100 \
  --lr 0.001 \
  --db-path ledgerlens.db \
  --model-dir models \
  --hidden-dim 64 \
  --num-layers 2 \
  --sequence-length 48
```

The autoencoder is trained on **clean** wallet sequences (risk_score < 20 for
≥ 30 days). It learns to reconstruct normal trading patterns; anomalous bot
sequences have higher reconstruction loss at inference time.

## API Endpoint

```
GET /temporal/analysis/{wallet}
```

Response:
```json
{
  "wallet": "GABCD...XYZ",
  "temporal_anomaly_score": 0.72,
  "arima_residual_score": 0.65,
  "lstm_reconstruction_loss": 0.81,
  "iat_variance_score": 0.91,
  "burst_synchrony_score": 0.42,
  "iat_cv": 0.09,
  "n_trades": 284,
  "sequence_plot_b64": "iVBORw0KGgo..."
}
```

The `sequence_plot_b64` field contains a base64-encoded PNG showing the
48-step log-amount and trade-count time series.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TEMPORAL_BIN_MINUTES` | `5` | Time series bin width in minutes |
| `TEMPORAL_SEQUENCE_LENGTH` | `48` | LSTM input sequence length |
| `TEMPORAL_ARIMA_FIT_WINDOW_DAYS` | `7` | ARIMA training window |
| `TEMPORAL_LSTM_MODEL_PATH` | `models/lstm_autoencoder.pt` | LSTM checkpoint |
| `TEMPORAL_ENABLED` | `true` | Enable temporal analysis |
| `TEMPORAL_MIN_TRADES_FOR_ANALYSIS` | `10` | Minimum trades to analyse |
