---
title: "Implement Graph Neural Network Ring Detection Engine with PyTorch Geometric"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

LedgerLens currently uses Tarjan's strongly connected component algorithm to identify wash rings. While effective for closed loops, SCC cannot identify open wash structures (chain laundering, star-hub relay networks) or rank ring-member confidence. A Graph Neural Network trained on labelled transaction subgraphs replaces and extends the SCC heuristic — classifying each wallet node as a wash-ring member with a confidence score, capturing structural patterns SCC cannot express.

## Background & Context

The current `detection/ring_detection.py` constructs a directed graph of trades, then runs SCC to find cycles with ≥ 3 nodes all belonging to the same trade cluster. This approach has three weaknesses:

1. **Open structures**: a wash operation using relay wallets (`A → B → C → D → A`, with C and D as throwaway relays) appears as a 4-node ring. But `A → B → C`, `A → D → C` (star hub through C) has no cycle yet is clearly coordinated. SCC misses it.
2. **Confidence**: SCC is binary — a wallet is in a ring or it is not. There is no confidence gradient to surface borderline cases.
3. **Temporal structure**: SCC ignores the time ordering of edges. A GNN can incorporate edge timestamps as features, capturing patterns like "all trades in this ring happened within a 30-minute window across multiple days."

A GNN addresses all three: it learns node embeddings that reflect the local and global topology of the transaction graph, and a binary classifier head outputs a `ring_membership_score` in [0, 1].

`detection/gnn_ring_detector.py` is a stub. This issue is the full implementation.

## Objectives

- [ ] Implement `TransactionGraphBuilder` that constructs a PyTorch Geometric `HeteroData` graph from a `(start_ts, end_ts)` trade window
- [ ] Implement a 3-layer GraphSAGE encoder (`GraphSAGEEncoder`) that produces 64-dim node embeddings
- [ ] Implement `RingMembershipClassifier` — a 2-layer MLP head that outputs `ring_membership_score` ∈ [0, 1]
- [ ] Implement `GNNRingDetector` that wraps encoder + classifier, exposes `predict(wallet: str) → float`
- [ ] Build training script `scripts/train_gnn.py` using the labelled `ring_members` table as ground truth
- [ ] Replace `wash_ring_membership` feature in `FEATURE_NAMES` with the GNN score (keep SCC as a fallback when GNN is not fitted)
- [ ] Expose `GET /gnn/ring-score/{wallet}` returning score and top-5 neighbouring wallets by embedding cosine similarity
- [ ] Write tests covering graph construction, forward pass shape, and the SCC fallback path

## Technical Requirements

### Graph schema

```python
# detection/gnn_ring_detector.py

from torch_geometric.data import HeteroData
import torch

def build_transaction_graph(
    trades: list[Trade],
    node_feature_fn: callable,  # wallet → torch.Tensor (shape: [n_node_features])
) -> HeteroData:
    """
    Nodes: wallets (base_asset buyer/seller)
    Edges: directed per trade, with edge features:
      - log_amount (float): log10 of trade base_amount
      - time_delta (float): seconds since first trade in window, normalised to [0, 1]
      - same_asset (bool): 1.0 if both legs use same base asset
    Returns HeteroData with node_types=['wallet'] and edge_types=[('wallet','trades','wallet')]
    """
    ...
```

### GraphSAGE encoder

```python
from torch_geometric.nn import SAGEConv
import torch.nn as nn

class GraphSAGEEncoder(nn.Module):
    def __init__(
        self,
        in_channels: int,       # number of node input features
        hidden_channels: int = 128,
        out_channels: int = 64,
        num_layers: int = 3,
        dropout: float = 0.3,
    ):
        super().__init__()
        self.convs = nn.ModuleList()
        self.convs.append(SAGEConv(in_channels, hidden_channels))
        for _ in range(num_layers - 2):
            self.convs.append(SAGEConv(hidden_channels, hidden_channels))
        self.convs.append(SAGEConv(hidden_channels, out_channels))
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, edge_index):
        for conv in self.convs[:-1]:
            x = conv(x, edge_index).relu()
            x = self.dropout(x)
        return self.convs[-1](x, edge_index)
```

### Classifier head

```python
class RingMembershipClassifier(nn.Module):
    def __init__(self, embedding_dim: int = 64):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(embedding_dim, 32),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

    def forward(self, embeddings: torch.Tensor) -> torch.Tensor:
        """Returns ring_membership_score ∈ [0, 1] per node."""
        return self.mlp(embeddings).squeeze(-1)
```

### GNNRingDetector wrapper

```python
class GNNRingDetector:
    MODEL_PATH = "models/gnn_ring_detector.pt"

    def __init__(self, fallback_to_scc: bool = True): ...

    def load(self) -> None:
        """Load encoder + classifier weights from MODEL_PATH. Sets self._fitted = True."""
        ...

    def predict(self, wallet: str, graph: HeteroData) -> float:
        """
        Returns ring_membership_score ∈ [0, 1].
        Falls back to SCC binary score (0.0 or 1.0) if not fitted and fallback_to_scc=True.
        """
        if not self._fitted:
            if self.fallback_to_scc:
                return float(self._scc_membership(wallet, graph))
            return 0.0
        ...

    def top_neighbours(self, wallet: str, graph: HeteroData, k: int = 5) -> list[str]:
        """Return k wallets with highest cosine similarity to wallet's embedding."""
        ...
```

### Training script

```python
# scripts/train_gnn.py
# Usage: python scripts/train_gnn.py --epochs 50 --lr 0.001 --neg-sample-ratio 3
#
# Ground truth: ring_members table (wallet, confirmed: bool)
# Negative examples: randomly sampled wallets with risk_score < 20 for at least 30 days
# Loss: binary cross-entropy with pos_weight = neg_sample_ratio
# Early stopping: patience=5 on validation AUC-ROC
# Saves encoder + classifier to models/gnn_ring_detector.pt
```

### Configuration

```
GNN_ENABLED=true
GNN_MODEL_PATH=models/gnn_ring_detector.pt
GNN_EMBEDDING_DIM=64
GNN_HIDDEN_CHANNELS=128
GNN_NUM_LAYERS=3
GNN_DROPOUT=0.3
GNN_RING_SCORE_THRESHOLD=0.5   # score above this → ring_membership feature = 1
GNN_FALLBACK_TO_SCC=true       # use SCC when GNN not fitted
```

## Security Considerations

- **Model file integrity**: `models/gnn_ring_detector.pt` must be verified against a SHA-256 checksum stored separately (`models/gnn_ring_detector.sha256`) before loading. Reject and fall back to SCC if checksum fails — prevents a compromised model file from altering detection outcomes
- **Training data poisoning**: negative sampling must exclude wallets with any open alert in the last 90 days, not just those with `risk_score < 20`, to avoid contaminating the negative class with unconfirmed wash traders
- **Inference isolation**: the GNN encoder runs as a separate in-process module, not a child process. Do not use `torch.load` on user-supplied model files; only load from the `GNN_MODEL_PATH` configured at startup
- **Embedding leakage via API**: `GET /gnn/ring-score/{wallet}` returns the score and top-5 neighbours but never the raw embedding vector. Exposing raw embeddings would allow adversaries to reconstruct the feature space and engineer evasion inputs

## Testing Requirements

- [ ] `tests/test_gnn_ring_detector.py`
- [ ] Test: `build_transaction_graph` with 10 wallets and 20 trades produces a `HeteroData` with correct node/edge counts
- [ ] Test: `GraphSAGEEncoder` forward pass produces output shape `(N_wallets, 64)` for any valid graph
- [ ] Test: `RingMembershipClassifier` output is in [0, 1] for all inputs
- [ ] Test: `GNNRingDetector.predict` returns SCC fallback value (0.0 or 1.0) when model is not fitted and `GNN_FALLBACK_TO_SCC=true`
- [ ] Test: `GNNRingDetector.predict` returns `0.0` when not fitted and `GNN_FALLBACK_TO_SCC=false`
- [ ] Test: `top_neighbours` returns exactly k wallets sorted by descending cosine similarity
- [ ] Test: checksum mismatch on `MODEL_PATH` triggers fallback, not exception
- [ ] Integration test: `GET /gnn/ring-score/{wallet}` returns correct schema with `score` and `top_neighbours` fields

## Documentation Requirements

- [ ] Docstrings on `TransactionGraphBuilder`, `GraphSAGEEncoder`, `RingMembershipClassifier`, `GNNRingDetector`
- [ ] `docs/gnn_ring_detection.md`: motivation (SCC limitations), graph schema, training procedure, evaluation metrics (AUC-ROC target ≥ 0.85 on held-out test set), fallback policy
- [ ] `scripts/train_gnn.py` includes a `--help` output documenting all flags
- [ ] Update `docs/detection_pipeline.md` to show the GNN as the primary ring detector with SCC as fallback
- [ ] Update `.env.example` with all six new configuration variables

## Definition of Done

- [ ] `GNNRingDetector`, `GraphSAGEEncoder`, `RingMembershipClassifier`, and `TransactionGraphBuilder` fully implemented
- [ ] `wash_ring_membership` feature sourced from GNN score (with SCC fallback)
- [ ] `GET /gnn/ring-score/{wallet}` endpoint live
- [ ] Training script functional and documented
- [ ] All tests pass including fallback path
- [ ] `docs/gnn_ring_detection.md` authored
- [ ] Model checksum verification implemented

## For Contributors

**Ideal contributor profile**: You have hands-on experience building and training graph neural networks with PyTorch Geometric or DGL. You understand GraphSAGE, message-passing architectures, and node classification tasks. Experience with financial transaction graph analysis, fraud detection on graph-structured data, or wash trading detection is a strong advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "graph neural networks for fraud detection", "transaction graph analysis", "PyTorch Geometric"
2. **Relevant experience** — GNN models you have trained for classification; any transaction graph or financial fraud detection work; experience with PyTorch Geometric's `HeteroData` API
3. **Approach / initial thoughts** — your view on GraphSAGE vs GCN vs GAT for this use case; how you would handle class imbalance in the ring/non-ring labels; thoughts on the SCC fallback design
4. **Estimated time** — breakdown by component (graph builder, encoder, classifier, detector wrapper, training script, API, tests, docs)
