---
title: "Build a Temporal Sequence Model for Sequential Trade Pattern Detection"
labels: ["difficulty: advanced", "area: ml", "type: feature"]
assignees: []
---

## Summary
The current LedgerLens feature set aggregates trade history into scalar statistics per rolling window (count, volume, Benford metrics), discarding the sequential structure of individual trades. Wash-trading bots exhibit characteristic temporal patterns — regular timing intervals, alternating buy/sell sequences, burst-then-pause cycles — that are invisible to aggregated features but detectable by a model that processes the ordered sequence of trades directly. This issue implements an LSTM or Transformer-based sequence model in `detection/temporal_model.py` that encodes a wallet's trade sequence into a contextual embedding, fused with the tabular feature vector for final risk scoring.

## Background & Context
`detection/feature_engineering.py` extracts aggregate statistics that summarise a wallet's trading behaviour over fixed windows, but the sequence of individual trades contains rich temporal structure:
- **Regular spacing**: wash-trading bots often execute trades every N seconds with very low jitter (unlike human traders with irregular timing)
- **Alternating direction**: classic wash trades alternate buy→sell→buy→sell in the same asset pair
- **Amount patterns**: fixed lot sizes or incrementing amounts to slightly vary the Benford distribution
- **Burst-pause cycles**: bots may execute 50 trades in 5 minutes, then pause for exactly 30 minutes, then repeat

An LSTM processes a trade sequence `[t_1, t_2, ..., t_N]` where each token represents one trade as a feature vector `[amount, direction, interarrival_time, asset_pair_hash, counterparty_hash]`. The LSTM hidden state at the last timestep is the sequence embedding. Alternatively, a Transformer encoder with positional encoding can be used, with the [CLS] token embedding as the sequence representation.

The sequence embedding is concatenated with the tabular feature vector from `feature_engineering.py` and passed to the final classifier, allowing the model to use both aggregate statistics and sequential structure.

## Objectives
- [ ] Implement `WashTradeSequenceModel` class in `detection/temporal_model.py` as a PyTorch `nn.Module` with configurable architecture (LSTM or Transformer encoder) and a fusion layer that concatenates the sequence embedding with the tabular feature vector
- [ ] Implement `TradeSequenceEncoder` that converts a `List[Trade]` (from `ingestion/data_models.py`) into a padded tensor suitable for batch training, with per-trade features: `[log_amount, direction_binary, log_interarrival_seconds, asset_pair_id, normalised_hour_of_day]`
- [ ] Train `WashTradeSequenceModel` on the synthetic labelled dataset using the temporal split (ISSUE-027); target max sequence length of 200 trades per wallet
- [ ] Fuse `WashTradeSequenceModel` output with tabular ensemble probability in `detection/model_inference.py` using a learned weight (same fusion mechanism as GNN in ISSUE-034, with independent `w_seq` weight)

## Technical Requirements

**Per-trade feature vector (5 dimensions):**
```python
def trade_to_feature_vector(trade: Trade, prev_trade: Optional[Trade], asset_pair_vocab: Dict[str, int]) -> np.ndarray:
    log_amount = np.log1p(abs(trade.amount))  # log(1+x) handles zero amounts
    direction = 1.0 if trade.base_is_buyer else 0.0
    log_interarrival = np.log1p(
        trade.timestamp - prev_trade.timestamp if prev_trade else 0.0
    )
    asset_id = asset_pair_vocab.get(trade.asset_pair, 0) / max(len(asset_pair_vocab), 1)  # normalised
    hour_of_day = (trade.timestamp % 86400) / 86400  # fraction of day
    return np.array([log_amount, direction, log_interarrival, asset_id, hour_of_day], dtype=np.float32)
```

**`WashTradeSequenceModel` architecture (LSTM variant):**
```python
class WashTradeSequenceModel(nn.Module):
    def __init__(
        self,
        trade_feature_dim: int = 5,
        tabular_feature_dim: int = 35,   # len(FEATURE_NAMES)
        lstm_hidden_dim: int = 64,
        lstm_num_layers: int = 2,
        fusion_hidden_dim: int = 32,
        dropout: float = 0.3,
        max_seq_len: int = 200,
    ):
        self.lstm = nn.LSTM(
            input_size=trade_feature_dim,
            hidden_size=lstm_hidden_dim,
            num_layers=lstm_num_layers,
            batch_first=True,
            dropout=dropout,
            bidirectional=False,
        )
        self.fusion = nn.Sequential(
            nn.Linear(lstm_hidden_dim + tabular_feature_dim, fusion_hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(fusion_hidden_dim, 1),
            nn.Sigmoid(),
        )

    def forward(self, sequences: Tensor, seq_lengths: Tensor, tabular: Tensor) -> Tensor:
        # Pack padded sequences for efficiency
        packed = pack_padded_sequence(sequences, seq_lengths.cpu(), batch_first=True, enforce_sorted=False)
        _, (h_n, _) = self.lstm(packed)
        seq_embedding = h_n[-1]  # last layer, shape (batch, lstm_hidden_dim)
        fused = torch.cat([seq_embedding, tabular], dim=1)
        return self.fusion(fused).squeeze(-1)
```

**`WashTradeSequenceModel` architecture (Transformer variant, selectable):**
```python
class TransformerSequenceModel(nn.Module):
    def __init__(self, trade_feature_dim=5, tabular_feature_dim=35, d_model=64, nhead=4, num_layers=2, ...):
        self.input_proj = nn.Linear(trade_feature_dim, d_model)
        self.pos_encoding = LearnedPositionalEncoding(d_model, max_len=200)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model))
        encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, batch_first=True)
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.fusion = ...  # same as LSTM variant
```

Select architecture via `TEMPORAL_MODEL_TYPE: str = "lstm"` in `config/settings.py`.

**Training configuration:**
```python
TRAINING:
  epochs: 100
  batch_size: 64
  learning_rate: 1e-3
  weight_decay: 1e-4
  optimizer: AdamW
  loss: BCELoss with pos_weight
  early_stopping: patience=15 on validation AUC-PR
  clip_grad_norm: 1.0  # critical for LSTM stability
  scheduler: ReduceLROnPlateau(patience=5, factor=0.5)
```

**Sequence truncation/padding strategy:**
- Truncate sequences longer than `max_seq_len=200` to the most recent 200 trades (recency bias is appropriate — recent behaviour is more predictive)
- Pad shorter sequences with zero-vectors and create a boolean `padding_mask` for the Transformer attention
- For the LSTM, use `pack_padded_sequence` to skip padding efficiently

**`TradeSequenceEncoder` batch construction:**
```python
def encode_batch(wallet_trades: Dict[str, List[Trade]], max_seq_len: int = 200) -> Tuple[Tensor, Tensor]:
    """Returns (padded_sequences, lengths) tensors for batch training."""
    sequences = []
    lengths = []
    for wallet, trades in wallet_trades.items():
        trades_sorted = sorted(trades, key=lambda t: t.timestamp)[-max_seq_len:]
        features = [trade_to_feature_vector(t, trades_sorted[i-1] if i > 0 else None, vocab)
                    for i, t in enumerate(trades_sorted)]
        seq = np.zeros((max_seq_len, 5), dtype=np.float32)
        seq[:len(features)] = features
        sequences.append(seq)
        lengths.append(len(features))
    return torch.tensor(np.stack(sequences)), torch.tensor(lengths)
```

**Fusion weight optimisation:**
Same approach as GNN (ISSUE-034): use `scipy.optimize.minimize_scalar` on validation AUC-PR to find `w_seq ∈ [0.0, 0.4]`.

**Model persistence:**
- Save to `models/temporal_model.pt` using `torch.save(model.state_dict(), path)` + `torch.load(path, weights_only=True)`
- Add to `/health` endpoint model file check

## Security Considerations
- `torch.load(path, weights_only=True)` is mandatory (same concern as GNN — pickle deserialization)
- `temporal_model.pt` must be signed with Ed25519 (ISSUE-035)
- Input sequence tensors must be constructed from validated `Trade` objects (Pydantic-validated); raw Horizon API dicts must not be passed directly to `trade_to_feature_vector()`
- The `asset_pair_vocab` dict maps asset pair strings to integer IDs; it must be persisted alongside the model (`models/asset_pair_vocab.json`) and loaded at inference time — a mismatch between training and inference vocabularies would cause silent feature corruption

## Testing Requirements
- Unit tests covering:
  - `trade_to_feature_vector()`: `log_amount` correct for amount=0 (`np.log1p(0) = 0`), amount=100 (≈4.615)
  - `encode_batch()`: sequences longer than 200 are truncated to 200; shorter sequences are zero-padded to 200
  - `WashTradeSequenceModel.forward()`: input `(batch=4, seq=200, feat=5)` + tabular `(4, 35)` → output `(4,)` in [0, 1]
  - `pack_padded_sequence` with varying lengths: no error when lengths differ
- Integration tests covering:
  - Full training run (5 epochs) on synthetic sequence data: loss decreases, no NaN
  - `models/temporal_model.pt` created after training
  - `ModelInference.score()` with sequence model loaded: returns valid `RiskScore`
  - Inference without `temporal_model.pt`: falls back to tabular ensemble gracefully
- Edge cases:
  - Wallet with 1 trade: sequence length 1, padded to 200; valid forward pass
  - All trades in sequence have identical timestamps: `log_interarrival = log1p(0) = 0` for all steps
  - Wallet with no trades: empty sequence → zero tensor → baseline prediction from tabular features only

## Documentation Requirements
- Create `detection/temporal_model.py` with comprehensive docstrings for `WashTradeSequenceModel` and `TradeSequenceEncoder`
- Add `TEMPORAL_MODEL_TYPE`, `TEMPORAL_MAX_SEQ_LEN`, `TEMPORAL_LSTM_HIDDEN_DIM` to `config/settings.py`
- Update `README.md` to mention the temporal sequence model in the Features section
- Create `docs/temporal_model.md` explaining LSTM vs Transformer trade-offs, the per-trade feature encoding, and the fusion strategy

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: PyTorch LSTM/Transformer, sequence modelling, `pack_padded_sequence`, time-series fraud detection
- Your approach or initial thoughts on the LSTM vs Transformer choice for this task
- Estimated time to complete

**Ideal contributor profile:** Deep learning engineer with production experience training sequence models on financial transaction data; familiarity with variable-length sequence handling in PyTorch (`pack_padded_sequence`, attention masking) is essential.
