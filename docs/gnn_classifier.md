# GNN-Based Wash Ring Classifier

## Overview

LedgerLens augments its tabular ML ensemble with a Graph Neural Network (GNN)
that operates directly on the trade graph topology. While the tabular ensemble
treats each wallet's graph features as independent inputs, the GNN learns
multi-hop structural patterns — nested rings, coordinated multi-asset wash
cycles — that are invisible to node-level features alone.

## Architecture

The `WashRingGNN` class supports two convolution types:

### GraphSAGE (default)
- `SAGEConv` layers aggregate neighbour features using mean pooling
- Better suited for inductive learning on evolving trade graphs
- Lower computational cost than attention-based alternatives

### GAT (Graph Attention Network)
- `GATConv` layers use multi-head attention to weight neighbour contributions
- Useful when some counterparties are more informative (e.g., a central
  wash-ring coordinator contributes more signal than peripheral wallets)
- Set `conv_type="gat"` and `gat_heads=4` (default)

### Network Structure
```
Input (node features) → [SAGEConv/GATConv + BatchNorm + ReLU + Dropout] × 2
                       → Linear(hidden, 32) → ReLU → Dropout → Linear(32, 1) → Sigmoid
```

Two message-passing hops capture 3-4 node rings without over-smoothing.

## Training

```python
from detection.gnn_model import WashRingGNN, TradeGraphDataset, train_wash_ring_gnn

dataset = TradeGraphDataset(graph, feature_vectors, labels)
model = train_wash_ring_gnn(dataset, epochs=200, patience=20)
```

Training configuration:
- **Loss**: BCEWithLogitsLoss with `pos_weight` = class imbalance ratio
- **Optimizer**: Adam (lr=1e-3, weight_decay=1e-4)
- **Scheduler**: CosineAnnealingLR (T_max=200)
- **Early stopping**: patience=20 epochs on training loss

## Fusion Strategy

The GNN output probability is fused with the tabular ensemble using a
learned scalar weight `w_gnn`:

```
fused_score = (1 - w_gnn) * tabular_prob + w_gnn * gnn_prob
```

`w_gnn` is optimised on the validation set via bounded scalar minimisation
of negative AUC-PR, constrained to [0.0, 0.5]. The optimal weight is
persisted in `training_metadata.json`.

Default `w_gnn = 0.2` when no optimised weight is available.

## Fallback Behaviour

If `wash_ring_gnn.pt` is absent or PyTorch is not installed, inference
falls back to the tabular-only ensemble without raising an error. An INFO
log message is emitted: "GNN model not available; using tabular ensemble only".

## Dependencies

```bash
pip install -e ".[gnn]"
```

This installs `torch>=2.0.0` and `torch_geometric>=2.5.0`. The rest of
LedgerLens functions without these packages installed.

## Security

- `torch.load()` uses `weights_only=True` to prevent arbitrary code execution
  via pickle deserialization
- GNN model weights are stored alongside tabular model artifacts and should
  be signed using the same model signing infrastructure
- Node indices in `edge_index` cannot be used to reconstruct wallet identity
  outside the model inference context

## Performance Targets

| Operation | Target | Hardware |
|-----------|--------|----------|
| Training (200 epochs, 10K nodes, 50K edges) | < 10 min | CPU |
| Training (same) | < 2 min | GPU |
| Inference (single forward pass) | < 10 ms | CPU |
