# Temporal Sequence Model

## Overview

`detection/temporal_model.py` implements a sequence model that encodes a wallet's
ordered trade history into a contextual embedding, fused with the tabular feature
vector from `feature_engineering.py` for final wash-trade risk scoring.

The motivation: wash-trading bots exhibit characteristic temporal patterns invisible
to aggregate statistics — regular inter-trade intervals, alternating buy/sell
sequences, fixed lot sizes, and burst-pause cycles. A model that processes the
ordered sequence of individual trades can detect these signals directly.

## Architecture

### Per-trade feature vector (5 dimensions)

Each trade is encoded as:

| Index | Feature | Description |
|-------|---------|-------------|
| 0 | `log_amount` | `log1p(|base_amount|)` — compresses large amounts, handles zero |
| 1 | `direction` | `1.0` if base is buyer, `0.0` if seller |
| 2 | `log_interarrival` | `log1p(seconds since previous trade)` — compresses timing |
| 3 | `asset_id` | Normalised integer ID from the asset-pair vocabulary |
| 4 | `hour_of_day` | Fraction of UTC day — captures off-hours activity |

### LSTM variant (`WashTradeSequenceModel`)

```
trades [T₁, T₂, …, Tₙ]
       ↓  (trade_to_feature_vector)
sequences  (batch, max_seq_len=200, 5)
       ↓  pack_padded_sequence
LSTM  (2 layers, hidden=64, dropout=0.3)
       ↓  h_n[-1]  (last layer hidden state)
seq_embedding  (batch, 64)
       ↓  torch.cat
[seq_embedding ‖ tabular_features]  (batch, 64+35=99)
       ↓  fusion MLP (99→32→ReLU→Dropout→1→Sigmoid)
wash_trade_probability  (batch,)  ∈ [0,1]
```

**Why LSTM?**
- Variable-length sequences handled natively via `pack_padded_sequence`.
- Captures long-range temporal dependencies (burst-pause cycles spanning hundreds of trades).
- Well-understood; easier to debug vanishing gradients than attention in small models.
- Lower memory footprint than Transformer on short sequences.

### Transformer variant (`TransformerSequenceModel`)

An alternative architecture using a learned [CLS] token and standard
`TransformerEncoderLayer`. Select via `TEMPORAL_MODEL_TYPE=transformer` in `.env`.

```
trades → input_proj (5→64)
       ↓  prepend [CLS] token
       ↓  LearnedPositionalEncoding
TransformerEncoder  (2 layers, nhead=4, dropout=0.3)
       ↓  CLS output embedding
[CLS_embedding ‖ tabular_features]  (batch, 64+35)
       ↓  fusion MLP
wash_trade_probability
```

**When to prefer Transformer:**
- Sequences are long (100–200 trades) and distant patterns matter more than recent context.
- You have enough GPU memory and training data to support attention's quadratic scaling.
- Interpretability via attention weights is a priority.

## Sequence truncation and padding

- Sequences longer than `TEMPORAL_MAX_SEQ_LEN` (default 200) are **truncated to the
  most recent 200 trades** — recency bias is appropriate since recent behaviour is
  more predictive of current wash-trading activity.
- Shorter sequences are **zero-padded** to `max_seq_len`.
- For the LSTM, `pack_padded_sequence` skips the padded positions efficiently.
- For the Transformer, a boolean `src_key_padding_mask` prevents attention over
  padding positions.

## Fusion with tabular ensemble

After training, the sequence model probability is blended with the tabular
ensemble probability using a learned scalar weight `w_seq`:

```
final_prob = (1 - w_seq) × tabular_prob + w_seq × seq_prob
```

`w_seq` is found by `scipy.optimize.minimize_scalar` on validation AUC-PR,
constrained to [0.0, 0.4] to prevent the sequence model from dominating when
its training data is limited. The `fuse_sequence_score` function in
`detection/model_inference.py` implements this blend.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPORAL_MODEL_TYPE` | `lstm` | `"lstm"` or `"transformer"` |
| `TEMPORAL_MAX_SEQ_LEN` | `200` | Max trades per sequence |
| `TEMPORAL_LSTM_HIDDEN_DIM` | `64` | LSTM hidden / Transformer d_model dimension |

## Model persistence

```
models/
├── temporal_model.pt        ← state_dict saved with torch.save(weights_only=True)
└── asset_pair_vocab.json    ← asset-pair string → integer ID mapping
```

**Security**: `torch.load` is always called with `weights_only=True` to prevent
arbitrary code execution via pickle deserialization.

**Vocabulary consistency**: the `asset_pair_vocab` must be identical between
training and inference. A mismatch causes silent feature corruption (wrong
asset_id values). Always load the vocab from `models/asset_pair_vocab.json`
rather than rebuilding it at inference time.

## TradeSequenceEncoder

`TradeSequenceEncoder` is the bridge between raw `List[Trade]` and padded tensors:

```python
encoder = TradeSequenceEncoder(max_seq_len=200)
sequences, lengths = encoder.encode_batch({"wallet_A": trades_A, "wallet_B": trades_B})
# sequences: (2, 200, 5)
# lengths:   (2,) — actual per-wallet trade count, clamped to [1, 200]
```

## Training configuration

| Hyperparameter | Value |
|----------------|-------|
| Epochs | 100 (early stop patience=15 on val AUC-PR) |
| Batch size | 64 |
| Optimiser | AdamW, lr=1e-3, weight_decay=1e-4 |
| Loss | BCELoss with `pos_weight` for class imbalance |
| Gradient clipping | `clip_grad_norm` = 1.0 (critical for LSTM stability) |
| LR schedule | `ReduceLROnPlateau(patience=5, factor=0.5)` |
