"""Temporal Graph Attention Network (TGAT) for wash-ring detection.

Architecture rationale (2 hops): SDEX wash rings detected so far are
predominantly small cycles (3-6 wallets). Two GAT message-passing hops let
a wallet's representation incorporate its direct counterparties and its
counterparties' counterparties -- enough to capture a 3-4 node ring without
the over-smoothing that emerges past 3-4 hops on these sparse, low-diameter
trade graphs.

Reference: Xu, D. et al. (2020) "Inductive Representation Learning on
Temporal Graphs" (TGAT), ICLR.
"""
from __future__ import annotations

import logging
import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch_geometric.nn import GATConv
    _HAS_PYG = True
except ImportError:
    torch = None
    nn = None
    F = None
    GATConv = None
    _HAS_PYG = False

logger = logging.getLogger(__name__)

NODE_FEATURE_DIM = 4
TIME_ENCODING_DIM = 16
HIDDEN_DIM = 32
DEFAULT_HOPS = 2


if _HAS_PYG:

    class TimeEncoder(nn.Module):
        """Functional time encoding from the TGAT paper (Xu et al. 2020)."""

        def __init__(self, dim: int = TIME_ENCODING_DIM) -> None:
            super().__init__()
            self.dim = dim
            self.w = nn.Parameter(torch.from_numpy(1.0 / 10 ** np.linspace(0, 9, dim)).float())
            self.b = nn.Parameter(torch.zeros(dim))

        def forward(self, delta_t):
            return torch.cos(delta_t.unsqueeze(-1) * self.w + self.b)

    class TGATLayer(nn.Module):
        """One temporal graph attention hop."""

        def __init__(self, in_dim: int, out_dim: int, heads: int = 4) -> None:
            super().__init__()
            self.time_encoder = TimeEncoder()
            self.gat = GATConv(
                in_dim, out_dim, heads=heads, concat=False,
                edge_dim=3 + TIME_ENCODING_DIM, add_self_loops=True,
            )

        def forward(self, x, edge_index, edge_attr, edge_time):
            t_enc = self.time_encoder(edge_time)
            edge_features = torch.cat([edge_attr, t_enc], dim=-1)
            return F.elu(self.gat(x, edge_index, edge_attr=edge_features))

    class TGATWashRingDetector(nn.Module):
        """Stacked TGAT hops -> per-wallet wash-ring probability head."""

        def __init__(self, node_in_dim: int = NODE_FEATURE_DIM,
                     hidden_dim: int = HIDDEN_DIM, n_hops: int = DEFAULT_HOPS) -> None:
            super().__init__()
            self.n_hops = n_hops
            self.input_proj = nn.Linear(node_in_dim, hidden_dim)
            self.layers = nn.ModuleList([TGATLayer(hidden_dim, hidden_dim) for _ in range(n_hops)])
            self.head = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim // 2),
                nn.ReLU(),
                nn.Linear(hidden_dim // 2, 1),
            )

        def forward(self, x, edge_index, edge_attr, edge_time):
            """Returns (N, 1) wash-ring probability per node, in [0, 1]."""
            h = F.relu(self.input_proj(x))
            for layer in self.layers:
                h = layer(h, edge_index, edge_attr, edge_time)
            logits = self.head(h)
            return torch.sigmoid(logits)

        def neighbor_avg_score(self, scores, edge_index, n_nodes):
            """Mean wash-ring score of each node's direct in-neighbors."""
            avg = torch.zeros(n_nodes, 1, device=scores.device)
            counts = torch.zeros(n_nodes, 1, device=scores.device)
            if edge_index.shape[1] == 0:
                return avg.squeeze(-1)
            src, dst = edge_index
            avg.index_add_(0, dst, scores[src])
            counts.index_add_(0, dst, torch.ones_like(scores[src]))
            counts = counts.clamp(min=1.0)
            return (avg / counts).squeeze(-1)

else:

    class TGATWashRingDetector:  # type: ignore
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch / torch_geometric not installed.")


def safe_load_gnn_checkpoint(path: str, model=None):
    """Loads a T-GNN checkpoint safely using weights_only=True (never False --
    same vulnerability class as issue #32)."""
    if not _HAS_PYG:
        raise RuntimeError("PyTorch / torch_geometric not installed.")

    checkpoint = torch.load(path, weights_only=True, map_location="cpu")
    expected_dim = checkpoint.get("node_in_dim", NODE_FEATURE_DIM)
    if expected_dim != NODE_FEATURE_DIM:
        raise RuntimeError(
            f"GNN checkpoint expects node feature dim {expected_dim}, but "
            f"current schema is {NODE_FEATURE_DIM}. Retrain before loading."
        )

    if model is None:
        model = TGATWashRingDetector(
            node_in_dim=expected_dim,
            hidden_dim=checkpoint.get("hidden_dim", HIDDEN_DIM),
            n_hops=checkpoint.get("n_hops", DEFAULT_HOPS),
        )
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return model


def save_gnn_checkpoint(model, path: str) -> None:
    """Saves a T-GNN checkpoint with schema metadata for safe-load validation."""
    torch.save(
        {
            "state_dict": model.state_dict(),
            "node_in_dim": NODE_FEATURE_DIM,
            "hidden_dim": HIDDEN_DIM,
            "n_hops": model.n_hops,
        },
        path,
    )
