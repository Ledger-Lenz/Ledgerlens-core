---
title: "Integrate Graph Neural Network (GNN) for Structural Wash-Ring Scoring"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Integrate `detection/gnn_model.py` into the inference pipeline. The GNN takes the Stellar trade graph (nodes=wallets, edges=trade volume/count) and outputs a per-node wash-ring probability that augments the SCC-based graph features already computed by `detection/graph_engine.py`. Use PyTorch Geometric with a GraphSAGE architecture. The GNN captures higher-order structural patterns — multi-hop neighbourhoods, hub-and-spoke topologies, clique sub-structures — that the current 4-feature SCC model cannot represent.

## Background & Context

LedgerLens's current graph-based features (`wash_ring_membership`, `wash_ring_size`, `cycle_volume_ratio`, `timing_tightness_score`) are derived from Tarjan's SCC algorithm, which finds strongly connected components but is binary: a wallet is either in a ring or it is not. This misses:

1. **Near-ring structures**: wallets that are one hop outside a ring but route value into it (feeder nodes)
2. **Hub topology**: a single wallet trading with 50 distinct counterparties in a star pattern — not a ring, but highly suspicious
3. **Multi-ring participation**: wallets that bridge two separate wash rings, acting as a coordinator

GraphSAGE (Hamilton et al., 2017) aggregates neighbourhood information iteratively: at each layer, a node's representation is updated by aggregating its neighbours' representations. A 3-layer GraphSAGE trained on labelled wash-trade graphs can learn all three structural patterns above.

`detection/gnn_model.py` exists as a stub. This issue is the full production implementation, training pipeline, and inference integration.

## Objectives

- [ ] Implement `WashRingGNN` model class using `torch_geometric.nn.SAGEConv` with 3 message-passing layers
- [ ] Implement `TradeGraphDataset` that converts the `NetworkX` trade graph from `graph_engine.py` into a `torch_geometric.data.Data` object
- [ ] Implement `GNNTrainer` that trains on labelled synthetic trade graphs (from `synthetic_data.py`) with node-level binary cross-entropy loss
- [ ] Implement `GNNInferenceEngine` that scores all nodes in the current trade graph and returns per-wallet wash-ring probabilities
- [ ] Add `gnn_wash_ring_prob` as a new ML feature in `feature_engineering.py` and `FEATURE_NAMES`
- [ ] Integrate `GNNInferenceEngine` into the main scoring pipeline (`run_pipeline.py`)
- [ ] Persist trained GNN as `models/gnn_v{hash}.pt` alongside RF/XGBoost/LightGBM artifacts
- [ ] Expose `GET /admin/gnn-stats` returning model architecture summary and last inference time

## Technical Requirements

### Model architecture

```python
# detection/gnn_model.py

import torch
import torch.nn.functional as F
from torch_geometric.nn import SAGEConv

class WashRingGNN(torch.nn.Module):
    def __init__(
        self,
        in_channels: int,       # number of node features
        hidden_channels: int = 64,
        num_layers: int = 3,
        dropout: float = 0.3,
    ):
        super().__init__()
        self.convs = torch.nn.ModuleList()
        self.convs.append(SAGEConv(in_channels, hidden_channels))
        for _ in range(num_layers - 2):
            self.convs.append(SAGEConv(hidden_channels, hidden_channels))
        self.convs.append(SAGEConv(hidden_channels, 1))  # binary output
        self.dropout = dropout

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        """
        x: node feature matrix, shape (N, in_channels)
        edge_index: edge list, shape (2, E)
        Returns: node-level logits, shape (N, 1)
        """
        for conv in self.convs[:-1]:
            x = conv(x, edge_index)
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        return self.convs[-1](x, edge_index)
```

### Node features

Each node (wallet) in the graph has the following features (shape `in_channels = 7`):

| Index | Feature | Source |
|-------|---------|--------|
| 0 | `log1p(out_degree)` | trade graph |
| 1 | `log1p(in_degree)` | trade graph |
| 2 | `log1p(total_volume)` | trade graph |
| 3 | `volume_to_unique_counterparty_ratio` | feature_engineering |
| 4 | `round_trip_trade_frequency` | feature_engineering |
| 5 | `wash_ring_membership` | graph_engine (SCC) |
| 6 | `account_age_days_normalized` | account_loader |

```python
class TradeGraphDataset:
    def __init__(self, graph_engine: GraphEngine, feature_engine: FeatureEngine): ...

    def to_pyg_data(self) -> "torch_geometric.data.Data":
        """
        Convert NetworkX trade graph to PyG Data.
        Node ordering is deterministic (sorted wallet addresses).
        Edge weights are log1p(total_volume).
        """
        import torch
        from torch_geometric.data import Data

        nodes = sorted(self._graph.nodes())
        node_idx = {w: i for i, w in enumerate(nodes)}
        x = torch.tensor(self._build_node_features(nodes), dtype=torch.float)
        edges = [(node_idx[u], node_idx[v]) for u, v in self._graph.edges()]
        edge_index = torch.tensor(edges, dtype=torch.long).t().contiguous()
        return Data(x=x, edge_index=edge_index)
```

### Training

```python
class GNNTrainer:
    def __init__(
        self,
        model: WashRingGNN,
        lr: float = 1e-3,
        epochs: int = 100,
        early_stopping_patience: int = 10,
        device: str = "cpu",
    ): ...

    def train(
        self,
        data: "torch_geometric.data.Data",
        y: torch.Tensor,          # node-level labels {0, 1}
        val_mask: torch.Tensor,   # boolean mask for validation nodes
    ) -> dict:
        """
        Train with node-level binary cross-entropy.
        Early stopping on validation AUC-ROC.
        Returns training history dict.
        """
        ...

    def save(self, path: Path, version_hash: str) -> None:
        torch.save({
            "model_state": self.model.state_dict(),
            "version_hash": version_hash,
            "in_channels": self.model.convs[0].in_channels,
            "hidden_channels": self.model.convs[0].out_channels,
            "num_layers": len(self.model.convs),
        }, path)
```

### Inference engine

```python
class GNNInferenceEngine:
    def __init__(self, model_path: Path, device: str = "cpu"): ...

    def score_graph(
        self, data: "torch_geometric.data.Data", node_wallets: list[str]
    ) -> dict[str, float]:
        """
        Run forward pass; apply sigmoid to logits.
        Returns {wallet: wash_ring_prob} for all nodes.
        """
        self._model.eval()
        with torch.no_grad():
            logits = self._model(data.x, data.edge_index)
            probs = torch.sigmoid(logits).squeeze(1).cpu().numpy()
        return {w: float(p) for w, p in zip(node_wallets, probs)}
```

### Feature integration

```python
# detection/feature_engineering.py
FEATURE_NAMES = [
    # ... existing features ...
    "gnn_wash_ring_prob",    # Feature 40: 0.0–1.0
]
```

### Configuration

```
GNN_HIDDEN_CHANNELS=64
GNN_NUM_LAYERS=3
GNN_DROPOUT=0.3
GNN_LR=0.001
GNN_EPOCHS=100
GNN_EARLY_STOPPING_PATIENCE=10
GNN_DEVICE=cpu
```

## Security Considerations

- **Model file integrity**: `gnn_v{hash}.pt` must be SHA-256 verified on load (same pattern as other model files in `model_registry.py`). Reject and raise `IntegrityError` if the hash does not match
- **Adversarial graph injection**: an attacker could add many low-volume edges to dilute their neighbourhood representation. The GraphSAGE mean aggregator is robust to this by design (mean over neighbours, not sum), but document this in the model's docstring
- **CUDA device security**: the `GNN_DEVICE` env var must only accept `"cpu"` or `"cuda"` (validate with an allowlist); reject arbitrary strings to prevent path traversal via PyTorch device parsing
- **Graph size bounds**: for very large graphs (> 100,000 nodes), full-batch inference may OOM. Add a `max_batch_nodes` parameter and fall back to mini-batch inference using `torch_geometric.loader.NeighborLoader` if the graph exceeds this threshold
- **Model versioning**: always load the GNN version hash from `models/gnn_latest.txt` (same convention as RF/XGBoost); never hardcode a version string in source code

## Testing Requirements

- [ ] `tests/test_gnn_model.py` — unit tests for model, dataset, trainer, and inference engine
- [ ] Test: `WashRingGNN` forward pass produces output shape `(N, 1)` for a 10-node graph
- [ ] Test: `TradeGraphDataset.to_pyg_data()` produces correct `edge_index` shape and node feature matrix
- [ ] Test: `GNNTrainer.train()` on synthetic 100-node graph completes without error and returns `val_auc > 0.5`
- [ ] Test: `GNNInferenceEngine.score_graph()` returns probabilities in `[0, 1]` for all nodes
- [ ] Test: model save/load round-trip produces identical inference output (within 1e-6 tolerance)
- [ ] Test: `max_batch_nodes` guard triggers mini-batch inference for large graphs
- [ ] Test: `gnn_wash_ring_prob` feature is non-zero for wallets in labelled wash rings
- [ ] Integration test: `run_pipeline.py` with GNN enabled completes and populates `gnn_wash_ring_prob` in scored feature vectors

## Documentation Requirements

- [ ] Docstrings on `WashRingGNN`, `TradeGraphDataset`, `GNNTrainer`, `GNNInferenceEngine`
- [ ] Add `docs/gnn_model.md` explaining GraphSAGE architecture, the 7 node features, training procedure, early stopping, and when to retrain (after each drift-detected retrain cycle)
- [ ] Update `README.md` ML layer section to mention the GNN alongside RF/XGBoost/LightGBM
- [ ] Update the feature table with `gnn_wash_ring_prob`
- [ ] Update `.env.example` with GNN configuration variables

## Definition of Done

- [ ] `WashRingGNN`, `TradeGraphDataset`, `GNNTrainer`, `GNNInferenceEngine` fully implemented
- [ ] `gnn_wash_ring_prob` in `FEATURE_NAMES` and computed in `feature_engineering.py`
- [ ] GNN artifact saved/loaded with SHA-256 integrity check
- [ ] `cli.py train` trains the GNN alongside the tabular ensemble
- [ ] All tests pass including integration test
- [ ] `docs/gnn_model.md` authored
- [ ] No new lint errors; `torch_geometric` added to `requirements.txt` with pinned version

## For Contributors

**Ideal contributor profile**: You have hands-on experience with PyTorch Geometric (or DGL) and have trained GNNs on real-world graphs. You understand GraphSAGE, message-passing neural networks, and node-level classification. Familiarity with the Stellar trade graph structure and the existing `graph_engine.py` SCC implementation is a significant advantage. Experience training models on class-imbalanced node-classification datasets (SMOTE or loss-weighting for rare positive nodes) is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "graph neural networks", "PyTorch Geometric", "fraud detection on transaction graphs"
2. **Relevant experience** — GNN models you have trained; specific graph fraud or anomaly detection work; GitHub repos or papers
3. **Approach / initial thoughts** — your view on GraphSAGE vs GAT or GIN for this task; how you would handle the class imbalance (wash nodes are rare); thoughts on mini-batch vs full-batch inference
4. **Estimated time** — breakdown by component (model, dataset, trainer, inference, integration, tests, docs)
