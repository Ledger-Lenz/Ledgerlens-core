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
| `top_neighbours` | Up to 5 most similar wallets **in the queried wallet's local ego-network** (last 500 trades involving the wallet). |
| `model_fitted` | `false` when score is from SCC fallback. |
| `fallback_used` | `true` when GNN model was not available. |

### `GET /v1/wallets/{wallet}/similar`

For globally similar wallets across the entire graph (not just the local ego-network), use the vector similarity API. See [docs/vector_similarity_search.md](./vector_similarity_search.md) for details.

## Local vs Global Similarity

| Aspect | Local (`top_neighbours` from `/gnn/ring-score`) | Global (`/v1/wallets/{wallet}/similar`) |
|--------|-------------------------------------------------|------------------------------------------|
| Scope | Queries only last 500 trades involving the wallet. | Queries all wallets with precomputed embeddings. |
| Use Case | Quick check of nearby, recently trading wallets. | Finding structurally similar siblings that have no direct trade relationship with the queried wallet. |
| Freshness | Computed on-the-fly every request. | Uses precomputed embeddings (updated via `cli.py compute-embeddings`). |
| Backend | Brute-force cosine similarity on small subgraph. | FAISS-based approximate nearest neighbor (ANN) search. |

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

## Heterogeneous Graph Mode

### Motivation

The homogeneous graph projects all trades into wallet-to-wallet edges, discarding
which asset pair was traded and any order-lifecycle context. This means:
- A ring laundering XLM → BRIDGE_ASSET → XLM across two pairs looks identical to
  a ring trading a single pair directly.
- Order cancellation patterns (create → cancel → trade at manipulated price) are
  invisible to the GNN.
- Funding-source relationships are not propagated through message passing.

### Schema

| Node Type | Features | Description |
|-----------|----------|-------------|
| `wallet` | 4-dim (hash-based) | Stellar wallet addresses |
| `asset` | 4-dim (hash-based) | Asset pair identifiers (e.g. XLM/USDC) |
| `order` | 4-dim (amount_norm, price_norm, side_buy, side_sell) | Order-book events |

| Edge Type | Source → Target | Description |
|-----------|----------------|-------------|
| `(wallet, trades, wallet)` | wallet → wallet | Direct trade edge (retained from homogeneous) |
| `(wallet, trades, asset)` | wallet → asset | Wallet participated in trade of this asset |
| `(asset, traded_by, wallet)` | asset → wallet | Reverse for bidirectional message passing |
| `(wallet, funds, wallet)` | wallet → wallet | Funding-source relationship from account_loader |
| `(wallet, creates, order)` | wallet → order | Offer creation event |
| `(wallet, cancels, order)` | wallet → order | Offer cancellation event |
| `(order, for, asset)` | order → asset | Which asset pair an order targets |

### HeteroConv vs HGTConv Tradeoff

| | HeteroConv + SAGEConv | HGTConv |
|---|---|---|
| **Mechanism** | Per-edge-type SAGEConv within `HeteroConv`, then mean aggregation across types | Multi-head heterogeneous graph transformer with learnable type-specific attention |
| **Parameters** | Fewer (one SAGEConv per edge type) | More (attention heads × type embeddings) |
| **Speed** | Faster — O(E × hidden) per layer | Slower — O(E × heads × hidden²) per layer |
| **Expressiveness** | Good for shared feature spaces | Better when node types have different feature distributions |
| **Recommended** | Default choice (`GNN_HETERO_CONV_TYPE=sage`) | Use when asset/order features diverge significantly from wallet features |

### Fallback Chain

```
heterogeneous → homogeneous → SCC
```

When `GNN_GRAPH_MODE=heterogeneous`:
1. Try to load hetero checkpoint from `GNN_HETERO_MODEL_PATH`.
2. If missing or checksum fails → log WARNING, fall back to homogeneous.
3. If homogeneous checkpoint also unavailable → fall back to SCC (if `GNN_FALLBACK_TO_SCC=true`).

### New GNN Features

The heterogeneous graph feeds three additional features into the tabular ensemble:

| Feature | Description |
|---------|-------------|
| `gnn_asset_mediated_ring_score` | Embedding-based score from asset-mediated paths (wallet→asset→wallet) |
| `gnn_order_cancel_coordination_score` | Score from coordinated order create/cancel timing patterns |
| `gnn_funding_proximity_score` | Score from funding-source graph proximity |

### Training

```bash
# Train heterogeneous model with SAGE conv (default)
python scripts/train_gnn.py --graph-mode heterogeneous --conv-type sage --epochs 50

# Train with HGT attention
python scripts/train_gnn.py --graph-mode heterogeneous --conv-type hgt --epochs 50

# Train homogeneous (default, backward-compatible)
python scripts/train_gnn.py --graph-mode homogeneous --epochs 50
```

### Configuration

Add to `.env`:

```env
GNN_GRAPH_MODE=homogeneous          # homogeneous | heterogeneous
GNN_HETERO_CONV_TYPE=sage           # sage | hgt
GNN_HETERO_MODEL_PATH=models/gnn_ring_detector_hetero.pt
GNN_HETERO_CHECKSUM_PATH=models/gnn_ring_detector_hetero.sha256
```

### Benchmark: Asset-Mediated Laundering

The `AssetMediatedProfile` in `ingestion/synthetic_data.py` generates a laundering
campaign that routes XLM → BRIDGE_ASSET → XLM across two asset pairs, with
coordinated order-book create+cancel events.

On this attack pattern (8 wallets, 100 wash trades, seed=42):
- **Homogeneous mode**: AUC-ROC ≈ 0.78 (limited by loss of asset identity)
- **Heterogeneous mode (SAGE)**: AUC-ROC ≈ 0.86+ (recovers asset-mediated paths)

The existing ≥0.85 AUC-ROC target on the standard round-trip benchmark is maintained.

## Detection Pipeline Integration

The `wash_ring_membership` feature in `FEATURE_NAMES` is sourced from:
1. `gnn_wash_ring_prob` (GNN score, if fitted and score ≥ `GNN_RING_SCORE_THRESHOLD`)
2. SCC binary membership (fallback)

The continuous GNN score is stored in `gnn_wash_ring_prob`. The binary `wash_ring_membership` feature is derived from it using the threshold, preserving backward compatibility with existing trained models.

See also: `docs/detection_pipeline.md`.
