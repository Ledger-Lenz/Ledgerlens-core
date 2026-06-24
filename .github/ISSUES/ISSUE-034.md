---
title: "Build a GNN-Based Wash Ring Classifier Using PyTorch Geometric"
labels: ["difficulty: advanced", "area: ml", "type: feature"]
assignees: []
---

## Summary
The current LedgerLens detection pipeline identifies wash rings through Tarjan's SCC algorithm in `detection/graph_engine.py` and extracts graph-structural features (ring membership, ring size, cycle volume ratio, timing tightness) for the tabular ML ensemble. This purely structural approach treats each wallet's graph features as independent tabular inputs, discarding the rich relational information encoded in the full trade graph topology. A Graph Neural Network (GNN) trained directly on the trade graph can learn complex multi-hop structural patterns (e.g., nested rings, coordinated multi-asset wash cycles) that are invisible to node-level tabular features alone. This issue implements a GraphSAGE or Graph Attention Network (GAT) classifier in `detection/gnn_model.py` using PyTorch Geometric and integrates it into the ensemble scoring pipeline.

## Background & Context
`detection/graph_engine.py` builds a directed weighted trade graph where nodes are Stellar account addresses and edges carry `total_volume`, `trade_count`, and timestamp information. The current pipeline extracts per-node features from this graph and passes them to the tabular ensemble. This discards structural information beyond the immediate SCC.

GNNs address this limitation by learning node representations that aggregate information from multi-hop neighbourhoods. For wash ring detection:
- A **GraphSAGE** layer aggregates neighbour features using mean/max/LSTM aggregation, producing node embeddings that encode the local trading neighbourhood
- A **GAT** layer uses attention weights to differentially weight neighbour contributions, which is useful when some trading counterparties are much more informative than others (e.g., a central wash-ring coordinator)
- Node-level wash-trading classification uses the node embedding as input to a 2-layer MLP classifier

The GNN output (wash-trading probability per node) is fused with the tabular ensemble output in `detection/model_inference.py` as an additional ensemble member with a learned fusion weight.

The trade graph must be converted to a PyTorch Geometric `Data` object with:
- `x`: node feature matrix (tabular features from `feature_engineering.py`)
- `edge_index`: COO-format directed edge list from `graph_engine.py`
- `edge_attr`: edge features `[total_volume, trade_count, timing_tightness]`
- `y`: node labels (wash-trading ground truth from `dataset.py`)

## Objectives
- [ ] Implement `WashRingGNN` class in `detection/gnn_model.py` using PyTorch Geometric's `SAGEConv` or `GATConv`, with 2 message-passing layers, a 2-layer MLP classifier head, and configurable hidden dimension
- [ ] Implement `TradeGraphDataset(InMemoryDataset)` that converts the trade graph from `graph_engine.py` into a PyTorch Geometric `Data` object, handling the node/edge feature construction
- [ ] Train `WashRingGNN` jointly with the tabular ensemble using the same temporal train/validation split (ISSUE-027); save trained GNN weights to `models/gnn_model.pt`
- [ ] Fuse GNN output probability with tabular ensemble probability in `detection/model_inference.py` using a learned scalar weight `w_gnn` (initialised to 0.2) optimised on the validation set

## Technical Requirements

**`WashRingGNN` model architecture:**
```python
import torch
import torch.nn.functional as F
from torch_geometric.nn import SAGEConv, GATConv, global_mean_pool

class WashRingGNN(torch.nn.Module):
    def __init__(
        self,
        in_channels: int,          # number of node features (= len(FEATURE_NAMES))
        hidden_channels: int = 64, # embedding dimension
        num_layers: int = 2,
        dropout: float = 0.3,
        conv_type: str = "sage",   # "sage" or "gat"
        gat_heads: int = 4,
    ):
        super().__init__()
        self.convs = torch.nn.ModuleList()
        self.bns = torch.nn.ModuleList()
        for i in range(num_layers):
            in_c = in_channels if i == 0 else hidden_channels
            if conv_type == "sage":
                self.convs.append(SAGEConv(in_c, hidden_channels))
            else:
                heads = gat_heads if i < num_layers - 1 else 1
                self.convs.append(GATConv(in_c, hidden_channels // heads, heads=heads, dropout=dropout))
            self.bns.append(torch.nn.BatchNorm1d(hidden_channels))
        self.classifier = torch.nn.Sequential(
            torch.nn.Linear(hidden_channels, 32),
            torch.nn.ReLU(),
            torch.nn.Dropout(dropout),
            torch.nn.Linear(32, 1),
        )

    def forward(self, x, edge_index, edge_attr=None):
        for conv, bn in zip(self.convs, self.bns):
            x = conv(x, edge_index)
            x = bn(x)
            x = F.relu(x)
            x = F.dropout(x, p=0.3, training=self.training)
        return torch.sigmoid(self.classifier(x)).squeeze(-1)
```

**`TradeGraphDataset` construction:**
```python
from torch_geometric.data import Data, InMemoryDataset

class TradeGraphDataset(InMemoryDataset):
    def process(self):
        graph = graph_engine.get_latest_graph()  # NetworkX DiGraph
        node_list = list(graph.nodes())
        node_to_idx = {n: i for i, n in enumerate(node_list)}
        # Node features: tabular feature vector from feature_engineering
        x = torch.tensor([feature_vectors[n] for n in node_list], dtype=torch.float)
        # Edge index: COO format
        edges = list(graph.edges(data=True))
        src = [node_to_idx[u] for u, v, _ in edges]
        dst = [node_to_idx[v] for _, v, _ in edges]
        edge_index = torch.tensor([src, dst], dtype=torch.long)
        # Edge attributes: [total_volume, trade_count, timing_tightness]
        edge_attr = torch.tensor(
            [[d["total_volume"], d["trade_count"], d.get("timing_tightness", 0.0)] for _, _, d in edges],
            dtype=torch.float,
        )
        y = torch.tensor([labels.get(n, 0) for n in node_list], dtype=torch.float)
        data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)
        self.save([data], self.processed_paths[0])
```

**Training configuration:**
```python
TRAINING:
  epochs: 200
  learning_rate: 1e-3
  weight_decay: 1e-4
  optimizer: Adam
  loss: BCELoss with pos_weight=imbalance_ratio (same as XGBoost scale_pos_weight)
  early_stopping: patience=20 epochs on validation AUC-PR
  scheduler: CosineAnnealingLR(T_max=200)
```

**Fusion weight optimisation:**
After all models are trained, find `w_gnn ∈ [0.0, 0.5]` that maximises AUC-PR on the validation set:
```python
from scipy.optimize import minimize_scalar

def fused_score(w_gnn, tabular_proba, gnn_proba):
    return (1 - w_gnn) * tabular_proba + w_gnn * gnn_proba

result = minimize_scalar(
    lambda w: -average_precision_score(y_val, fused_score(w, tabular_val, gnn_val)),
    bounds=(0.0, 0.5), method="bounded"
)
w_gnn_optimal = result.x  # persist to training_metadata.json
```

**Inference integration:**
```python
# In ModelInference.score():
if self.gnn_model is not None and graph_data is not None:
    gnn_proba = self.gnn_model(graph_data.x, graph_data.edge_index).detach().numpy()
    ensemble_p = (1 - self.w_gnn) * tabular_p + self.w_gnn * gnn_proba[wallet_node_idx]
```

**Fallback behaviour:**
- If `gnn_model.pt` is absent or GPU is unavailable, inference falls back to tabular-only ensemble without raising an error
- Log `INFO "GNN model not available; using tabular ensemble only"` when falling back

**Performance targets:**
- Training (200 epochs, 10,000 nodes, 50,000 edges): < 10 minutes on CPU, < 2 minutes on GPU
- Inference per wallet (single forward pass on subgraph): < 10 ms on CPU

**Dependencies:**
- `torch>=2.0.0` and `torch_geometric>=2.5.0` added to `requirements.txt` as optional dependencies
- Add a `[gnn]` extras group in `pyproject.toml`; the rest of LedgerLens must function without PyTorch installed

## Security Considerations
- GNN model weights (`gnn_model.pt`) must be signed alongside tabular model artifacts (see ISSUE-035)
- `torch.load()` has a known arbitrary code execution vulnerability via pickle; use `torch.load(path, weights_only=True)` (available in PyTorch ≥ 2.0) to restrict loading to tensor weights only
- The trade graph passed to the GNN contains wallet addresses; ensure node indices in `edge_index` cannot be used to reconstruct wallet identity outside the model inference context

## Testing Requirements
- Unit tests covering:
  - `WashRingGNN` forward pass: input `(x: [10, 35], edge_index: [2, 20])` → output `[10]` values in [0, 1]
  - `TradeGraphDataset`: converts a 5-node synthetic NetworkX graph to valid `Data` object with correct shapes
  - Fusion weight optimisation: finds `w_gnn` in [0.0, 0.5] without error
- Integration tests covering:
  - Full GNN training run (5 epochs) on synthetic graph: loss decreases, no NaN
  - `ModelInference.score()` with GNN loaded: returns valid `RiskScore`
  - `ModelInference.score()` without GNN (`gnn_model.pt` absent): falls back gracefully
- Edge cases:
  - Isolated nodes (no edges): GNN returns base feature embedding without message passing
  - Graph with a single node: valid forward pass
  - `edge_index` with self-loops: handled correctly by SAGEConv/GATConv

## Documentation Requirements
- Create `detection/gnn_model.py` with comprehensive docstrings for `WashRingGNN` and `TradeGraphDataset`
- Add `torch` and `torch_geometric` to `requirements.txt` with version pins and an `# optional: GNN` comment
- Update `README.md` Features section to mention the GNN-based ring classifier
- Create `docs/gnn_classifier.md` with architecture diagram, training procedure, and fusion strategy explanation

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: PyTorch Geometric, GraphSAGE/GAT, graph neural networks for fraud detection, PyTorch training loops
- Your approach or initial thoughts on the graph construction from the existing NetworkX graph
- Estimated time to complete

**Ideal contributor profile:** Deep learning engineer with hands-on PyTorch Geometric experience; experience applying GNNs to financial fraud detection graphs (transaction networks, account graphs) is highly valuable.
