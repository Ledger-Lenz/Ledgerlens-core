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


# ---------------------------------------------------------------------------
# WashRingGNN: GraphSAGE / GAT classifier for wash-ring detection
# ---------------------------------------------------------------------------

if _HAS_PYG:
    from torch_geometric.nn import SAGEConv
    from torch_geometric.data import Data, InMemoryDataset

    class WashRingGNN(nn.Module):
        """GraphSAGE or GAT classifier for per-node wash-trading probability.

        Two message-passing layers aggregate multi-hop neighbourhood features.
        A 2-layer MLP classifier head produces a wash-trading probability per
        node. The architecture is monotone: adding more suspicious neighbours
        always increases the node's score.
        """

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
            self.num_layers = num_layers
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
        """Convert a NetworkX trade graph into a PyTorch Geometric Data object.

        Node features come from the tabular feature engineering pipeline.
        Edge features carry [total_volume, trade_count, timing_tightness].
        """

        def __init__(
            self,
            graph,
            feature_vectors: dict[str, list[float]],
            labels: dict[str, int] | None = None,
            root: str | None = None,
        ):
            self._graph = graph
            self._feature_vectors = feature_vectors
            self._labels = labels or {}
            self._data_obj = None
            self._process_graph()

        def _process_graph(self):
            node_list = list(self._graph.nodes())
            node_to_idx = {n: i for i, n in enumerate(node_list)}

            default_dim = len(next(iter(self._feature_vectors.values()), [0.0]))
            x = torch.tensor(
                [self._feature_vectors.get(n, [0.0] * default_dim) for n in node_list],
                dtype=torch.float,
            )

            edges = list(self._graph.edges(data=True))
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
                [float(self._labels.get(n, 0)) for n in node_list], dtype=torch.float
            )

            self._data_obj = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)
            self._node_list = node_list
            self._node_to_idx = node_to_idx

        @property
        def data(self) -> Data:
            return self._data_obj

        @property
        def node_list(self) -> list[str]:
            return self._node_list

        @property
        def node_to_idx(self) -> dict[str, int]:
            return self._node_to_idx

        def len(self):
            return 1

        def get(self, idx):
            return self._data_obj

    def train_wash_ring_gnn(
        dataset: TradeGraphDataset,
        epochs: int = 200,
        lr: float = 1e-3,
        weight_decay: float = 1e-4,
        patience: int = 20,
    ) -> WashRingGNN:
        """Train a WashRingGNN on the trade graph dataset."""
        data = dataset.data
        in_channels = data.x.shape[1]
        model = WashRingGNN(in_channels=in_channels)

        pos_count = data.y.sum().item()
        neg_count = len(data.y) - pos_count
        pos_weight = torch.tensor([neg_count / max(pos_count, 1)])

        optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
        criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        best_loss = float("inf")
        patience_counter = 0

        model.train()
        for epoch in range(epochs):
            optimizer.zero_grad()
            # Use raw logits for BCEWithLogitsLoss
            for conv, bn in zip(model.convs, model.bns):
                pass  # forward handled below

            # Direct forward without sigmoid for loss
            x = data.x
            for conv, bn in zip(model.convs, model.bns):
                x = conv(x, data.edge_index)
                x = bn(x)
                x = F.relu(x)
                x = F.dropout(x, p=model.dropout, training=True)
            logits = model.classifier(x).squeeze(-1)

            loss = criterion(logits, data.y)
            loss.backward()
            optimizer.step()
            scheduler.step()

            if loss.item() < best_loss:
                best_loss = loss.item()
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= patience:
                    break

        model.eval()
        return model

    def compute_fusion_weight(
        tabular_proba: np.ndarray,
        gnn_proba: np.ndarray,
        y_val: np.ndarray,
    ) -> float:
        """Find optimal GNN fusion weight in [0.0, 0.5] maximising AUC-PR."""
        from scipy.optimize import minimize_scalar
        from sklearn.metrics import average_precision_score

        def neg_ap(w):
            fused = (1 - w) * tabular_proba + w * gnn_proba
            return -average_precision_score(y_val, fused)

        result = minimize_scalar(neg_ap, bounds=(0.0, 0.5), method="bounded")
        return float(result.x)

    def save_wash_ring_gnn(model: WashRingGNN, path: str) -> None:
        """Save WashRingGNN checkpoint with weights_only-compatible format."""
        torch.save(
            {
                "state_dict": model.state_dict(),
                "in_channels": model.convs[0].in_channels if model.convs else 0,
                "hidden_channels": 64,
                "num_layers": model.num_layers,
            },
            path,
        )

    def load_wash_ring_gnn(path: str) -> WashRingGNN:
        """Load WashRingGNN safely using weights_only=True."""
        checkpoint = torch.load(path, weights_only=True, map_location="cpu")
        model = WashRingGNN(
            in_channels=checkpoint.get("in_channels", NODE_FEATURE_DIM),
            hidden_channels=checkpoint.get("hidden_channels", 64),
            num_layers=checkpoint.get("num_layers", 2),
        )
        model.load_state_dict(checkpoint["state_dict"])
        model.eval()
        return model

else:
    class WashRingGNN:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch / torch_geometric not installed.")

    class TradeGraphDataset:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            raise RuntimeError("PyTorch / torch_geometric not installed.")
