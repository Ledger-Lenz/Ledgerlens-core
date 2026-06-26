"""Graph Neural Network models for wash-ring detection.

Contains two architectures:
1. TGATWashRingDetector — Temporal GAT (2-hop) for streaming inference
2. WashRingGNN — GraphSAGE/GAT classifier for ensemble fusion

Architecture rationale (2 hops): SDEX wash rings detected so far are
predominantly small cycles (3-6 wallets). Two message-passing hops let
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
    from torch_geometric.nn import GATConv, SAGEConv
    from torch_geometric.data import Data, InMemoryDataset
    _HAS_PYG = True
except ImportError:
    torch = None
    nn = None
    F = None
    GATConv = None
    SAGEConv = None
    Data = None
    InMemoryDataset = None
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


# ---------------------------------------------------------------------------
# WashRingGNN — GraphSAGE/GAT ensemble member
# ---------------------------------------------------------------------------

GNN_FUSION_WEIGHT_DEFAULT: float = 0.2

if _HAS_PYG:

    class WashRingGNN(nn.Module):
        """GraphSAGE or GAT classifier for wash-ring detection."""

        def __init__(
            self,
            in_channels: int,
            hidden_channels: int = 64,
            num_layers: int = 2,
            dropout: float = 0.3,
            conv_type: str = "sage",
            gat_heads: int = 4,
        ):
            super().__init__()
            self.conv_type = conv_type
            self.dropout = dropout
            self.convs = nn.ModuleList()
            self.bns = nn.ModuleList()
            for i in range(num_layers):
                in_c = in_channels if i == 0 else hidden_channels
                if conv_type == "sage":
                    self.convs.append(SAGEConv(in_c, hidden_channels))
                else:
                    heads = gat_heads if i < num_layers - 1 else 1
                    self.convs.append(
                        GATConv(in_c, hidden_channels // heads, heads=heads, dropout=dropout)
                    )
                self.bns.append(nn.BatchNorm1d(hidden_channels))
            self.classifier = nn.Sequential(
                nn.Linear(hidden_channels, 32),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(32, 1),
            )

        def forward(self, x, edge_index, edge_attr=None):
            for conv, bn in zip(self.convs, self.bns):
                x = conv(x, edge_index)
                x = bn(x)
                x = F.relu(x)
                x = F.dropout(x, p=self.dropout, training=self.training)
            return torch.sigmoid(self.classifier(x)).squeeze(-1)

    class TradeGraphDataset(InMemoryDataset):
        """Convert a NetworkX trade graph to a PyTorch Geometric Data object."""

        def __init__(self, root=None, graph=None, feature_vectors=None, labels=None):
            self._graph = graph
            self._feature_vectors = feature_vectors or {}
            self._labels = labels or {}
            self._data_obj = None
            if graph is not None:
                self._data_obj = self._build_data()

        def _build_data(self):
            graph = self._graph
            node_list = list(graph.nodes())
            node_to_idx = {n: i for i, n in enumerate(node_list)}

            n_features = 0
            for n in node_list:
                fv = self._feature_vectors.get(n, [])
                if isinstance(fv, dict):
                    fv = list(fv.values())
                n_features = max(n_features, len(fv))
                break

            x_data = []
            for n in node_list:
                fv = self._feature_vectors.get(n, [0.0] * max(n_features, 1))
                if isinstance(fv, dict):
                    fv = list(fv.values())
                if len(fv) < n_features:
                    fv = fv + [0.0] * (n_features - len(fv))
                x_data.append(fv)

            x = torch.tensor(x_data, dtype=torch.float)

            edges = list(graph.edges(data=True))
            if edges:
                src = [node_to_idx[u] for u, v, _ in edges]
                dst = [node_to_idx[v] for _, v, _ in edges]
                edge_index = torch.tensor([src, dst], dtype=torch.long)
                edge_attr = torch.tensor(
                    [
                        [
                            d.get("total_volume", 0.0),
                            d.get("trade_count", 0.0),
                            d.get("timing_tightness", 0.0),
                        ]
                        for _, _, d in edges
                    ],
                    dtype=torch.float,
                )
            else:
                edge_index = torch.zeros((2, 0), dtype=torch.long)
                edge_attr = torch.zeros((0, 3), dtype=torch.float)

            y = torch.tensor(
                [float(self._labels.get(n, 0)) for n in node_list],
                dtype=torch.float,
            )

            return Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)

        @property
        def data(self):
            return self._data_obj

        def __len__(self):
            return 1 if self._data_obj is not None else 0

        def __getitem__(self, idx):
            return self._data_obj

    def train_wash_ring_gnn(
        data: Data,
        in_channels: int | None = None,
        hidden_channels: int = 64,
        num_layers: int = 2,
        epochs: int = 200,
        lr: float = 1e-3,
        weight_decay: float = 1e-4,
        patience: int = 20,
        conv_type: str = "sage",
    ) -> WashRingGNN:
        """Train WashRingGNN on a Data object; returns the trained model."""
        if in_channels is None:
            in_channels = data.x.shape[1]

        model = WashRingGNN(
            in_channels=in_channels,
            hidden_channels=hidden_channels,
            num_layers=num_layers,
            conv_type=conv_type,
        )

        pos_count = data.y.sum().item()
        neg_count = len(data.y) - pos_count
        pos_weight = torch.tensor([neg_count / max(pos_count, 1.0)])
        criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

        best_loss = float("inf")
        patience_counter = 0

        model.train()
        for epoch in range(epochs):
            optimizer.zero_grad()
            out = model(data.x, data.edge_index, data.edge_attr)
            loss = criterion(out, data.y)
            loss.backward()
            optimizer.step()
            scheduler.step()

            if loss.item() < best_loss:
                best_loss = loss.item()
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= patience:
                    logger.info("Early stopping at epoch %d", epoch)
                    break

        model.eval()
        return model

    def optimize_fusion_weight(
        tabular_proba: np.ndarray,
        gnn_proba: np.ndarray,
        y_true: np.ndarray,
        bounds: tuple[float, float] = (0.0, 0.5),
    ) -> float:
        """Find optimal fusion weight w_gnn that maximises AUC-PR."""
        from scipy.optimize import minimize_scalar
        from sklearn.metrics import average_precision_score

        def neg_ap(w):
            fused = (1 - w) * tabular_proba + w * gnn_proba
            return -average_precision_score(y_true, fused)

        result = minimize_scalar(neg_ap, bounds=bounds, method="bounded")
        return float(result.x)

else:

    class WashRingGNN:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch / torch_geometric not installed.")

    class TradeGraphDataset:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch / torch_geometric not installed.")

    def train_wash_ring_gnn(*a, **k):
        raise RuntimeError("PyTorch / torch_geometric not installed.")

    def optimize_fusion_weight(*a, **k):
        raise RuntimeError("PyTorch / torch_geometric not installed.")
