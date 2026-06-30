# GNN Ring Detection

## Motivation — SCC Limitations

LedgerLens originally used Tarjan's Strongly Connected Component (SCC) algorithm to identify wash rings. SCC is effective for closed cycles but has three fundamental weaknesses:

| Weakness | Description |
|----------|-------------|
| **Open structures** | Star-hub relay networks (`A → B → C`, `A → D → C`) have no cycle yet exhibit clear coordination. SCC misses them entirely. |
| **Binary membership** | SCC is binary — a wallet is either in a ring or not. There is no confidence gradient for borderline cases. |
| **Ignores time** | SCC operates on static graph topology, ignoring trade timestamps. A bot executing all ring trades within a 30-minute window looks identical to legitimate trading spanning weeks. |

A Graph Neural Network addresses all three: it learns node embeddings from the full local topology (including open structures), produces a continuous confidence score in [0, 1], and can incorporate edge timestamp features.

## Architecture

```
Trades (list) ──→ TransactionGraphBuilder ──→ HeteroData graph
                                                     │
                                            GraphSAGEEncoder (3-layer)
                                                     │
                                            node embeddings (64-dim)
                                                     │
                                         RingMembershipClassifier (MLP)
                                                     │
                                         ring_membership_score ∈ [0, 1]
```

### Graph Schema

| Component | Description |
|-----------|-------------|
| **Nodes** | Unique wallet addresses (base_account + counter_account) |
| **Edges** | Directed per trade: seller/base → buyer/counter |
| **Edge features** | `log_amount` (log10 of trade amount), `time_delta` (normalised [0,1]), `same_asset` (1.0 if both legs same asset) |
| **Node features** | 4-dim vector from `node_feature_fn` (customisable; defaults to hash-based) |

### GraphSAGE Encoder

Three-layer GraphSAGE (`torch_geometric.nn.SAGEConv`) producing 64-dimensional node embeddings:
- Layer 1: `in_channels → 128`, ReLU, Dropout(0.3)
- Layer 2: `128 → 128`, ReLU, Dropout(0.3)
- Layer 3: `128 → 64` (output)

GraphSAGE was chosen over GCN for its inductive capability (it aggregates neighbourhood samples, so it generalises to unseen wallets at inference time) and over GAT because the trade graph is sparse and attention weights add little benefit while increasing memory usage.

### Ring Membership Classifier

Two-layer MLP:
```
64 → 32 (ReLU, Dropout(0.2)) → 1 (Sigmoid)
```
Output is `ring_membership_score ∈ [0, 1]`.

## Training Procedure

```bash
python scripts/train_gnn.py --epochs 50 --lr 0.001 --neg-sample-ratio 3
```

### Ground Truth

| Class | Source |
|-------|--------|
| **Positives** | `ring_members` table (`confirmed = 1`) |
| **Negatives** | Wallets with `risk_score < 20` for last 30 days AND no open alert in last 90 days |

### Class Imbalance

Negatives are downsampled to `len(positives) × neg_sample_ratio`.  Binary cross-entropy uses `pos_weight = neg_sample_ratio` to further compensate.

### Early Stopping

Patience = 5 epochs on validation AUC-ROC.  Training stops as soon as validation AUC-ROC has not improved for 5 consecutive epochs.

### Evaluation Target

AUC-ROC ≥ 0.85 on a held-out test set (20% of labelled examples).

### Output

The training script saves a checkpoint to `models/gnn_ring_detector.pt` and a SHA-256 checksum to `models/gnn_ring_detector.sha256`.

## SCC Fallback Policy

When the GNN model is not loaded (no checkpoint, checksum mismatch, or PyG unavailable):

| `GNN_FALLBACK_TO_SCC` | Behaviour |
|-----------------------|-----------|
| `true` (default) | `predict()` returns `1.0` if SCC flagged the wallet, `0.0` otherwise |
| `false` | `predict()` always returns `0.0` |

The fallback is logged at WARNING level. Detection quality degrades gracefully rather than failing hard.

## Model Security

| Concern | Mitigation |
|---------|------------|
| Tampered model file | SHA-256 checksum verified before `torch.load()`. Mismatch triggers SCC fallback immediately. |
| Evasion via embedding reconstruction | `GET /gnn/ring-score/{wallet}` returns score + top-5 neighbours only — raw embeddings are never exposed. |
| User-supplied model files | The model is loaded only from `GNN_MODEL_PATH` set at startup. `torch.load` is not called on API-supplied paths. |
| Training data poisoning | Negative sampling explicitly excludes wallets with any open alert in the last 90 days. |

## API Reference

### `GET /gnn/ring-score/{wallet}`

```json
{
  "wallet": "GABC...XYZ",
  "ring_membership_score": 0.842,
  "top_neighbours": [
    "GDEF...PQR",
    "GUVW...STU",
    ...
  ],
  "model_fitted": true,
  "fallback_used": false
}
```

| Field | Description |
|-------|-------------|
| `ring_membership_score` | GNN probability ∈ [0,1]. Above `GNN_RING_SCORE_THRESHOLD` (default 0.5) → ring member. |
| `top_neighbours` | Up to 5 most similar wallets by embedding cosine similarity. |
| `model_fitted` | `false` when score is from SCC fallback. |
| `fallback_used` | `true` when GNN model was not available. |

### `GET /gnn/health`

Returns model path, load status, and fallback configuration.

## Configuration

Add to `.env`:

```env
GNN_ENABLED=true
GNN_MODEL_PATH=models/gnn_ring_detector.pt
GNN_EMBEDDING_DIM=64
GNN_HIDDEN_CHANNELS=128
GNN_NUM_LAYERS=3
GNN_DROPOUT=0.3
GNN_RING_SCORE_THRESHOLD=0.5
GNN_FALLBACK_TO_SCC=true
```

## Detection Pipeline Integration

The `wash_ring_membership` feature in `FEATURE_NAMES` is sourced from:
1. `gnn_wash_ring_prob` (GNN score, if fitted and score ≥ `GNN_RING_SCORE_THRESHOLD`)
2. SCC binary membership (fallback)

The continuous GNN score is stored in `gnn_wash_ring_prob`. The binary `wash_ring_membership` feature is derived from it using the threshold, preserving backward compatibility with existing trained models.

See also: `docs/detection_pipeline.md`.
