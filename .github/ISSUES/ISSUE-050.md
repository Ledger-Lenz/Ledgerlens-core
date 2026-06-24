---
title: "Build LSTM Temporal Sequence Model for Trade-Timing Pattern Detection"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/temporal_model.py` with a production-quality LSTM/Transformer that ingests sequences of `(trade_amount, inter_arrival_time, counterparty_entropy)` tuples per wallet and outputs a timing-pattern anomaly score. The score feeds into `feature_engineering.py` as the new feature `temporal_anomaly_score`. Wash-trading bots exhibit highly regular inter-arrival times and low counterparty entropy — patterns invisible to scalar features but detectable by sequence models.

## Background & Context

The 35 baseline features in LedgerLens are all scalar aggregates over a time window. They capture what a wallet does (volume, concentration, ring membership) but not *how* it does it temporally. Wash-trading bots often have characteristic timing signatures:

- **Metronomic inter-arrival times**: human traders have high variance in trade timing; bots execute at near-fixed intervals (e.g., every 4 seconds ± 0.2s)
- **Low counterparty entropy**: bots rotate through a small fixed counterparty set, producing low Shannon entropy of counterparty distribution
- **Amount clustering**: bot amounts cluster at round numbers or follow a tight geometric progression

An LSTM (or lightweight Transformer encoder) trained on labelled sequences of these three signals can detect these patterns even when scalar features are carefully perturbed to evade detection. This is the core motivation for the temporal model: it is harder to simultaneously fool both the scalar and the sequence model.

`detection/temporal_model.py` exists as a stub. This issue is the full implementation.

## Objectives

- [ ] Define `TradeSequence` dataclass encoding a wallet's recent trades as a fixed-length sequence of `(amount_log, iat_log, cp_entropy)` tuples
- [ ] Implement `TemporalFeatureExtractor` that converts raw `Trade` records into `TradeSequence` objects
- [ ] Implement `TemporalAnomalyLSTM` (or Transformer variant) model with configurable sequence length (default 50 steps)
- [ ] Implement `TemporalTrainer` with SMOTE-style sequence augmentation for the minority wash class
- [ ] Implement `TemporalInferenceEngine` returning a `temporal_anomaly_score` (0–1) per wallet
- [ ] Add `temporal_anomaly_score` to `FEATURE_NAMES` in `feature_engineering.py`
- [ ] Integrate `TemporalInferenceEngine` into the main scoring pipeline
- [ ] Write tests including a synthetic "metronomic bot" scenario that produces `temporal_anomaly_score > 0.8`

## Technical Requirements

### TradeSequence

```python
# detection/temporal_model.py

from dataclasses import dataclass
import numpy as np
import math

@dataclass
class TradeSequence:
    wallet: str
    sequence: np.ndarray   # shape (T, 3): columns = [log1p(amount), log1p(iat_s), cp_entropy]
    label: int | None      # 1 = wash, 0 = clean, None = unlabelled

    @classmethod
    def from_trades(
        cls,
        wallet: str,
        trades: list[dict],   # sorted by timestamp ascending
        seq_len: int = 50,
        label: int | None = None,
    ) -> "TradeSequence":
        """
        Build fixed-length sequence. Pad with zeros at the start if < seq_len trades.
        Truncate to the most recent seq_len trades if more.
        inter_arrival_time: seconds between consecutive trades (0 for first).
        counterparty_entropy: Shannon entropy of counterparty distribution over
            a sliding 10-trade window ending at each trade.
        """
        ...
```

### Counterparty entropy computation

```python
def _counterparty_entropy(trades: list[dict], window: int = 10) -> list[float]:
    """
    For each trade i, compute H(counterparty distribution over trades[max(0,i-window):i+1]).
    H = -sum p_k log p_k. Returns list of floats, same length as trades.
    """
    entropies = []
    for i, _ in enumerate(trades):
        window_trades = trades[max(0, i - window + 1): i + 1]
        counterparties = [t["counter_account"] for t in window_trades]
        counts = {}
        for cp in counterparties:
            counts[cp] = counts.get(cp, 0) + 1
        n = len(counterparties)
        h = -sum((c / n) * math.log2(c / n) for c in counts.values() if c > 0)
        entropies.append(h)
    return entropies
```

### LSTM model

```python
import torch
import torch.nn as nn

class TemporalAnomalyLSTM(nn.Module):
    def __init__(
        self,
        input_size: int = 3,
        hidden_size: int = 64,
        num_layers: int = 2,
        dropout: float = 0.3,
        bidirectional: bool = False,
    ):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout if num_layers > 1 else 0.0,
            bidirectional=bidirectional,
            batch_first=True,
        )
        out_size = hidden_size * (2 if bidirectional else 1)
        self.head = nn.Sequential(
            nn.Linear(out_size, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: shape (B, T, 3)
        Returns: logits, shape (B, 1)
        Uses the last hidden state of the final LSTM layer.
        """
        _, (h_n, _) = self.lstm(x)
        last_hidden = h_n[-1]  # (B, hidden_size)
        return self.head(last_hidden)
```

### Transformer variant (alternative, same interface)

```python
class TemporalAnomalyTransformer(nn.Module):
    """
    Lightweight encoder-only Transformer for short sequences (T=50).
    4 heads, 2 encoder layers, d_model=32.
    Use nn.TransformerEncoder with nn.TransformerEncoderLayer.
    Mean-pool across time dimension before classification head.
    """
    ...
```

### Trainer

```python
class TemporalTrainer:
    def __init__(
        self,
        model: TemporalAnomalyLSTM | TemporalAnomalyTransformer,
        lr: float = 1e-3,
        epochs: int = 50,
        batch_size: int = 64,
        device: str = "cpu",
        class_weight_ratio: float = 5.0,   # weight for positive (wash) class
    ): ...

    def train(
        self,
        sequences: list[TradeSequence],
    ) -> dict:
        """
        Train with weighted binary cross-entropy (pos_weight=class_weight_ratio).
        Shuffle sequences; 80/20 train/val split.
        Returns training history.
        """
        ...
```

### Inference engine

```python
class TemporalInferenceEngine:
    def __init__(self, model_path: Path, seq_len: int = 50, device: str = "cpu"): ...

    def score_wallet(
        self, wallet: str, trades: list[dict]
    ) -> float:
        """
        Build TradeSequence from trades, run model forward pass.
        Returns temporal_anomaly_score in [0, 1].
        Returns 0.5 (neutral) if len(trades) < 5 (insufficient sequence).
        """
        ...
```

### Feature integration

```python
# detection/feature_engineering.py
FEATURE_NAMES = [
    # ... existing 40 features ...
    "temporal_anomaly_score",   # Feature 41
]
```

### Configuration

```
TEMPORAL_MODEL_TYPE=lstm          # "lstm" or "transformer"
TEMPORAL_SEQ_LEN=50
TEMPORAL_HIDDEN_SIZE=64
TEMPORAL_NUM_LAYERS=2
TEMPORAL_DROPOUT=0.3
TEMPORAL_LR=0.001
TEMPORAL_EPOCHS=50
TEMPORAL_BATCH_SIZE=64
TEMPORAL_CLASS_WEIGHT_RATIO=5.0
TEMPORAL_DEVICE=cpu
```

## Security Considerations

- **Sequence padding**: zero-padding for short sequences must not bias the model toward labelling short-history wallets as wash traders. Validate on synthetic data that a wallet with only 3 trades returns `temporal_anomaly_score ≈ 0.5` (neutral), not high
- **Model file security**: apply the same SHA-256 integrity check pattern as GNN and tabular models — verify hash on load, raise `IntegrityError` on mismatch
- **Input normalisation**: `log1p(amount)` and `log1p(iat_s)` can produce values > 20 for very large amounts/intervals. Apply `torch.clamp(x, -10, 10)` before the LSTM to prevent gradient explosion during inference (models trained on normalised data should never see out-of-distribution extremes, but be defensive)
- **Counterparty entropy from external data**: the `counter_account` field from Horizon must be validated (Stellar public key format) before being included in entropy calculation. Reject malformed values and count them as "unknown" (separate counterparty bucket)
- **Device string injection**: `TEMPORAL_DEVICE` must only accept `"cpu"` or `"cuda"` (allowlist); reject other values at startup

## Testing Requirements

- [ ] `tests/test_temporal_model.py` — unit tests for all components
- [ ] Test: `_counterparty_entropy` with 1 counterparty → entropy 0.0; uniform 10 counterparties → entropy ≈ 3.32 bits
- [ ] Test: `TradeSequence.from_trades` pads correctly for < seq_len trades; truncates for > seq_len
- [ ] Test: `TemporalAnomalyLSTM` forward pass produces shape `(B, 1)` for batch of 4 sequences
- [ ] Test: `TemporalTrainer.train` on 200 synthetic sequences completes without error and returns `val_auc > 0.5`
- [ ] Test: "metronomic bot" scenario — wallet with inter-arrival time = 4.0 ± 0.1s and counterparty_entropy < 0.5 → `temporal_anomaly_score > 0.8` after training on labelled data
- [ ] Test: wallet with < 5 trades → `score_wallet` returns exactly `0.5`
- [ ] Test: model save/load round-trip produces identical scores (within 1e-6)
- [ ] Benchmark: `score_wallet` for a 50-step sequence in < 10ms on CPU

## Documentation Requirements

- [ ] Docstrings on `TradeSequence`, `TemporalFeatureExtractor`, `TemporalAnomalyLSTM`, `TemporalAnomalyTransformer`, `TemporalTrainer`, `TemporalInferenceEngine`
- [ ] Add `docs/temporal_model.md` explaining the timing-attack model, the three input signals, LSTM vs Transformer tradeoffs, the cold-start (< 5 trades) policy, and retraining frequency
- [ ] Update `README.md` ML layer table to include the temporal model
- [ ] Update feature table with `temporal_anomaly_score`
- [ ] Update `.env.example` with temporal model configuration variables

## Definition of Done

- [ ] `TradeSequence`, `TemporalAnomalyLSTM`, `TemporalTrainer`, `TemporalInferenceEngine` fully implemented
- [ ] `temporal_anomaly_score` in `FEATURE_NAMES` and computed in `feature_engineering.py`
- [ ] `cli.py train` trains the temporal model alongside the tabular ensemble
- [ ] Metronomic bot test passes with score > 0.8
- [ ] Cold-start test passes (< 5 trades → 0.5)
- [ ] Model artifact integrity check implemented
- [ ] All tests pass; benchmark passes (< 10ms CPU inference)
- [ ] `docs/temporal_model.md` authored

## For Contributors

**Ideal contributor profile**: You have production experience training sequence models (LSTM, GRU, Transformer) for anomaly detection or fraud detection. You understand time-series feature engineering (inter-arrival times, entropy) and are comfortable with PyTorch's LSTM API, batch-first semantics, and training loops. Familiarity with class-imbalanced binary classification (weighted BCE, SMOTE for sequences) is expected. Experience applying sequence models to financial transaction data is a strong advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "sequence models for anomaly detection", "financial fraud with LSTMs", "transformer architectures"
2. **Relevant experience** — specific models you have trained for timing-pattern detection; GitHub repos or papers; production inference latency you have achieved
3. **Approach / initial thoughts** — your view on LSTM vs Transformer for length-50 sequences; how you handle the cold-start problem; thoughts on the three chosen input signals
4. **Estimated time** — breakdown by component (feature extractor, model, trainer, inference engine, integration, tests, docs)
