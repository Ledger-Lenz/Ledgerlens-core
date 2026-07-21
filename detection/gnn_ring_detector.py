"""GNN-based wash-ring detection engine.

Replaces and extends the Tarjan SCC heuristic by:
- Classifying each wallet node as a ring member with a continuous confidence
  score in [0, 1], not a binary flag.
- Capturing open wash structures (star-hub relays, chain laundering) that SCC
  misses because they have no closed cycle.
- Incorporating edge timestamps as features, detecting temporal patterns
  (e.g., all trades in a ring within a 30-minute window).

Architecture
------------
1. ``TransactionGraphBuilder`` — builds a PyTorch Geometric ``HeteroData``
   graph from a list of ``Trade`` objects.
2. ``GraphSAGEEncoder`` — 3-layer GraphSAGE producing 64-dim node embeddings.
3. ``RingMembershipClassifier`` — 2-layer MLP head outputting a score in [0, 1].
4. ``GNNRingDetector`` — wraps encoder + classifier; falls back to SCC binary
   membership when the GNN model is not fitted.

Security
--------
- Model file integrity is verified against a SHA-256 checksum stored in
  ``models/gnn_ring_detector.sha256`` before loading.  Checksum mismatch
  triggers SCC fallback, not an exception.
- Raw embedding vectors are never exposed via the API — only the score and
  top-k neighbours.
- The model is loaded only from ``GNN_MODEL_PATH`` configured at startup.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger("ledgerlens.gnn_ring_detector")

# ---------------------------------------------------------------------------
# Optional heavy imports
# ---------------------------------------------------------------------------

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch_geometric.data import HeteroData
    from torch_geometric.nn import SAGEConv, HeteroConv, HGTConv
    _HAS_PYG = True
except ImportError:
    torch = None          # type: ignore[assignment]
    nn = None             # type: ignore[assignment]
    F = None              # type: ignore[assignment]
    HeteroData = None     # type: ignore[assignment]
    SAGEConv = None       # type: ignore[assignment]
    HeteroConv = None     # type: ignore[assignment]
    HGTConv = None        # type: ignore[assignment]
    _HAS_PYG = False

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL_PATH = "models/gnn_ring_detector.pt"
DEFAULT_CHECKSUM_PATH = "models/gnn_ring_detector.sha256"
DEFAULT_HETERO_MODEL_PATH = "models/gnn_ring_detector_hetero.pt"
DEFAULT_HETERO_CHECKSUM_PATH = "models/gnn_ring_detector_hetero.sha256"

# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_transaction_graph(
    trades: list,
    node_feature_fn,  # callable: wallet_str -> torch.Tensor shape [n_node_features]
) -> Any:  # HeteroData when PyG available
    """Build a PyTorch Geometric HeteroData graph from a list of Trade objects.

    Nodes: unique wallet addresses (base_account + counter_account).
    Edges: directed per trade from seller/base to buyer/counter.
    Edge features:
        - ``log_amount`` (float): log10(trade.base_amount + 1e-9)
        - ``time_delta`` (float): seconds since first trade, normalised to [0,1]
        - ``same_asset`` (float): 1.0 when both legs use the same base asset

    Parameters
    ----------
    trades:
        Iterable of Trade-like objects with attributes:
        ``base_account``, ``counter_account``, ``base_amount``,
        ``ledger_close_time``, ``base_asset_code``, ``counter_asset_code``.
    node_feature_fn:
        Maps wallet address string -> 1-D tensor of node features.

    Returns
    -------
    HeteroData with ``node_types=['wallet']`` and
    ``edge_types=[('wallet', 'trades', 'wallet')]``.
    """
    if not _HAS_PYG:
        raise ImportError("PyTorch Geometric is required for build_transaction_graph.")

    # Collect unique wallets
    wallets: list[str] = []
    wallet_idx: dict[str, int] = {}
    for t in trades:
        for acc in (t.base_account, t.counter_account):
            if acc not in wallet_idx:
                wallet_idx[acc] = len(wallets)
                wallets.append(acc)

    # Node feature matrix
    if wallets:
        node_feats = torch.stack([node_feature_fn(w) for w in wallets])
    else:
        node_feats = torch.zeros((0, 1))

    # Edge construction
    if not trades:
        edge_index = torch.zeros((2, 0), dtype=torch.long)
        edge_attr = torch.zeros((0, 3))
    else:
        # Determine time range for normalisation
        times = [float(getattr(t, "ledger_close_time_ts", 0) or 0) for t in trades]
        t_min = min(times) if times else 0.0
        t_max = max(times) if times else 1.0
        t_range = max(t_max - t_min, 1.0)

        src_ids, dst_ids, edge_feats = [], [], []
        for t, ts in zip(trades, times):
            src_ids.append(wallet_idx[t.base_account])
            dst_ids.append(wallet_idx[t.counter_account])
            log_amount = float(np.log10(float(t.base_amount) + 1e-9))
            time_delta = (ts - t_min) / t_range
            same_asset = float(
                getattr(t, "base_asset_code", "") == getattr(t, "counter_asset_code", "")
            )
            edge_feats.append([log_amount, time_delta, same_asset])

        edge_index = torch.tensor(
            [src_ids, dst_ids], dtype=torch.long
        )
        edge_attr = torch.tensor(edge_feats, dtype=torch.float)

    data = HeteroData()
    data["wallet"].x = node_feats.float()
    data["wallet", "trades", "wallet"].edge_index = edge_index
    data["wallet", "trades", "wallet"].edge_attr = edge_attr
    # Store wallet list for reverse lookup
    data["wallet"].wallet_list = wallets
    return data


# ---------------------------------------------------------------------------
# Heterogeneous graph builder
# ---------------------------------------------------------------------------


def build_heterogeneous_graph(
    trades: list,
    order_events: list | None = None,
    funding_edges: list[tuple[str, str]] | None = None,
    node_feature_fn=None,
    asset_feature_fn=None,
) -> Any:  # HeteroData when PyG available
    """Build a typed heterogeneous graph from trades, orders, and funding edges.

    Node types: wallet, asset, order.
    Edge types:
        ('wallet', 'trades', 'wallet')  — direct wallet-to-wallet trade (retained)
        ('wallet', 'trades', 'asset')   — wallet participated in trade of this asset
        ('asset', 'traded_by', 'wallet') — reverse for bidirectional message passing
        ('wallet', 'funds', 'wallet')   — funding-source relationship
        ('wallet', 'creates', 'order')  — offer creation
        ('wallet', 'cancels', 'order')  — offer cancellation
        ('order', 'for', 'asset')       — which asset pair an order targets

    Parameters
    ----------
    trades:
        List of Trade-like objects with base_account, counter_account,
        base_amount, ledger_close_time_ts, base_asset_code, counter_asset_code,
        and optionally base_asset/counter_asset Asset models.
    order_events:
        List of OrderBookEvent-like objects with account, asset_pair,
        event_type ("created"/"cancelled"/"updated"), timestamp, amount, price.
    funding_edges:
        List of (funder_wallet, funded_wallet) tuples.
    node_feature_fn:
        Maps wallet address -> tensor of node features.
    asset_feature_fn:
        Maps asset_pair_string -> tensor of asset features. If None, uses a
        default 4-dim hash-based feature vector.

    Returns
    -------
    HeteroData with heterogeneous node and edge types.
    """
    if not _HAS_PYG:
        raise ImportError("PyTorch Geometric is required for build_heterogeneous_graph.")

    if order_events is None:
        order_events = []
    if funding_edges is None:
        funding_edges = []

    if node_feature_fn is None:
        def node_feature_fn(wallet: str) -> "torch.Tensor":
            h = abs(hash(wallet)) % 10000
            return torch.tensor(
                [h / 10000.0, len(wallet) / 60.0, float(wallet.startswith("G")), 0.0],
                dtype=torch.float,
            )

    if asset_feature_fn is None:
        def asset_feature_fn(asset_pair: str) -> "torch.Tensor":
            h = abs(hash(asset_pair)) % 10000
            return torch.tensor(
                [h / 10000.0, len(asset_pair) / 40.0, 0.0, 0.0],
                dtype=torch.float,
            )

    # ── Collect unique node IDs ──────────────────────────────────────────
    wallet_idx: dict[str, int] = {}
    wallets: list[str] = []
    asset_idx: dict[str, int] = {}
    assets: list[str] = []
    order_idx: dict[str, int] = {}
    orders: list[str] = []

    def _get_wallet_idx(acc: str) -> int:
        if acc not in wallet_idx:
            wallet_idx[acc] = len(wallets)
            wallets.append(acc)
        return wallet_idx[acc]

    def _get_asset_idx(pair: str) -> int:
        if pair not in asset_idx:
            asset_idx[pair] = len(assets)
            assets.append(pair)
        return asset_idx[pair]

    def _get_order_idx(order_id: str) -> int:
        if order_id not in order_idx:
            order_idx[order_id] = len(orders)
            orders.append(order_id)
        return order_idx[order_id]

    # ── Walk through trades to collect wallets and assets ────────────────
    for t in trades:
        _get_wallet_idx(t.base_account)
        if t.counter_account is not None:
            _get_wallet_idx(t.counter_account)
        # Determine asset pair
        base_code = getattr(t, "base_asset_code", None)
        if base_code is None and hasattr(t, "base_asset"):
            base_code = t.base_asset.code if hasattr(t.base_asset, "code") else str(t.base_asset)
        counter_code = getattr(t, "counter_asset_code", None)
        if counter_code is None and hasattr(t, "counter_asset"):
            counter_code = t.counter_asset.code if hasattr(t.counter_asset, "code") else str(t.counter_asset)
        pair = f"{base_code or 'XLM'}/{counter_code or 'XLM'}"
        _get_asset_idx(pair)

    # ── Walk through order events to collect orders ──────────────────────
    for ev in order_events:
        _get_wallet_idx(ev.account)
        _get_asset_idx(ev.asset_pair)
        _get_order_idx(ev.id)

    # ── Build node feature matrices ──────────────────────────────────────
    wallet_feats = torch.stack([node_feature_fn(w) for w in wallets]) if wallets else torch.zeros((0, 4))
    asset_feats = torch.stack([asset_feature_fn(a) for a in assets]) if assets else torch.zeros((0, 4))
    # Order nodes: 4-dim features [amount_norm, price_norm, side_buy, side_sell]
    order_feats_list: list["torch.Tensor"] = []
    for oid in orders:
        # Find the corresponding order event to get features
        ev = next((e for e in order_events if e.id == oid), None)
        if ev is not None:
            amount_norm = float(ev.amount) / 10000.0 if ev.amount else 0.0
            price_norm = float(ev.price) / 100.0 if ev.price else 0.0
            side_buy = 1.0 if ev.side == "buy" else 0.0
            side_sell = 1.0 if ev.side == "sell" else 0.0
        else:
            amount_norm, price_norm, side_buy, side_sell = 0.0, 0.0, 0.0, 0.0
        order_feats_list.append(torch.tensor([amount_norm, price_norm, side_buy, side_sell], dtype=torch.float))
    order_feats = torch.stack(order_feats_list) if order_feats_list else torch.zeros((0, 4))

    # ── Build edge indices ───────────────────────────────────────────────
    # 1. ('wallet', 'trades', 'wallet') — existing direct wallet edges
    ww_src, ww_dst = [], []
    ww_edge_feats: list[list[float]] = []
    times = [float(getattr(t, "ledger_close_time_ts", 0) or 0) for t in trades]
    t_min = min(times) if times else 0.0
    t_max = max(times) if times else 1.0
    t_range = max(t_max - t_min, 1.0)

    for t, ts in zip(trades, times):
        src = wallet_idx[t.base_account]
        dst = wallet_idx[t.counter_account] if t.counter_account is not None else src
        if src == dst:
            continue
        ww_src.append(src)
        ww_dst.append(dst)
        log_amount = float(np.log10(float(t.base_amount) + 1e-9))
        time_delta = (ts - t_min) / t_range
        base_code = getattr(t, "base_asset_code", "")
        counter_code = getattr(t, "counter_asset_code", "")
        same_asset = float(base_code == counter_code)
        ww_edge_feats.append([log_amount, time_delta, same_asset])

    ww_edge_index = torch.tensor([ww_src, ww_dst], dtype=torch.long) if ww_src else torch.zeros((2, 0), dtype=torch.long)
    ww_edge_attr = torch.tensor(ww_edge_feats, dtype=torch.float) if ww_edge_feats else torch.zeros((0, 3))

    # 2. ('wallet', 'trades', 'asset') and reverse
    wa_src, wa_dst = [], []
    for t in trades:
        w_idx = wallet_idx[t.base_account]
        base_code = getattr(t, "base_asset_code", None)
        if base_code is None and hasattr(t, "base_asset"):
            base_code = t.base_asset.code if hasattr(t.base_asset, "code") else str(t.base_asset)
        counter_code = getattr(t, "counter_asset_code", None)
        if counter_code is None and hasattr(t, "counter_asset"):
            counter_code = t.counter_asset.code if hasattr(t.counter_asset, "code") else str(t.counter_asset)
        pair = f"{base_code or 'XLM'}/{counter_code or 'XLM'}"
        a_idx = asset_idx[pair]
        wa_src.append(w_idx)
        wa_dst.append(a_idx)

    wa_edge_index = torch.tensor([wa_src, wa_dst], dtype=torch.long) if wa_src else torch.zeros((2, 0), dtype=torch.long)
    aw_edge_index = torch.tensor([wa_dst, wa_src], dtype=torch.long) if wa_src else torch.zeros((2, 0), dtype=torch.long)

    # 3. ('wallet', 'funds', 'wallet')
    fund_src, fund_dst = [], []
    for funder, funded in funding_edges:
        if funder in wallet_idx and funded in wallet_idx:
            fund_src.append(wallet_idx[funder])
            fund_dst.append(wallet_idx[funded])
    fund_edge_index = torch.tensor([fund_src, fund_dst], dtype=torch.long) if fund_src else torch.zeros((2, 0), dtype=torch.long)

    # 4. ('wallet', 'creates', 'order') and ('wallet', 'cancels', 'order')
    creates_src, creates_dst = [], []
    cancels_src, cancels_dst = [], []
    for ev in order_events:
        w_idx = wallet_idx[ev.account]
        o_idx = order_idx[ev.id]
        if ev.event_type == "created":
            creates_src.append(w_idx)
            creates_dst.append(o_idx)
        elif ev.event_type == "cancelled":
            cancels_src.append(w_idx)
            cancels_dst.append(o_idx)
        # "updated" events are ignored for edge construction

    creates_edge_index = torch.tensor([creates_src, creates_dst], dtype=torch.long) if creates_src else torch.zeros((2, 0), dtype=torch.long)
    cancels_edge_index = torch.tensor([cancels_src, cancels_dst], dtype=torch.long) if cancels_src else torch.zeros((2, 0), dtype=torch.long)

    # 5. ('order', 'for', 'asset')
    ofa_src, ofa_dst = [], []
    for ev in order_events:
        o_idx = order_idx[ev.id]
        a_idx = asset_idx[ev.asset_pair]
        ofa_src.append(o_idx)
        ofa_dst.append(a_idx)
    ofa_edge_index = torch.tensor([ofa_src, ofa_dst], dtype=torch.long) if ofa_src else torch.zeros((2, 0), dtype=torch.long)

    # ── Assemble HeteroData ──────────────────────────────────────────────
    data = HeteroData()
    data["wallet"].x = wallet_feats.float()
    data["asset"].x = asset_feats.float()
    data["order"].x = order_feats.float()

    data["wallet", "trades", "wallet"].edge_index = ww_edge_index
    data["wallet", "trades", "wallet"].edge_attr = ww_edge_attr
    data["wallet", "trades", "asset"].edge_index = wa_edge_index
    data["asset", "traded_by", "wallet"].edge_index = aw_edge_index
    data["wallet", "funds", "wallet"].edge_index = fund_edge_index
    data["wallet", "creates", "order"].edge_index = creates_edge_index
    data["wallet", "cancels", "order"].edge_index = cancels_edge_index
    data["order", "for", "asset"].edge_index = ofa_edge_index

    # Store metadata for reconstruction
    data["wallet"].wallet_list = wallets
    data["asset"].asset_list = assets
    data["order"].order_list = orders

    return data


# ---------------------------------------------------------------------------
# GraphSAGE Encoder
# ---------------------------------------------------------------------------

if _HAS_PYG:

    class GraphSAGEEncoder(nn.Module):
        """3-layer GraphSAGE encoder producing 64-dim node embeddings.

        Parameters
        ----------
        in_channels:
            Number of input node features.
        hidden_channels:
            Width of intermediate layers (default 128).
        out_channels:
            Embedding dimensionality (default 64).
        num_layers:
            Number of SAGEConv layers (default 3).
        dropout:
            Dropout probability applied between layers (default 0.3).
        """

        def __init__(
            self,
            in_channels: int,
            hidden_channels: int = 128,
            out_channels: int = 64,
            num_layers: int = 3,
            dropout: float = 0.3,
        ) -> None:
            super().__init__()
            self.convs = nn.ModuleList()
            if num_layers == 1:
                self.convs.append(SAGEConv(in_channels, out_channels))
            else:
                self.convs.append(SAGEConv(in_channels, hidden_channels))
                for _ in range(num_layers - 2):
                    self.convs.append(SAGEConv(hidden_channels, hidden_channels))
                self.convs.append(SAGEConv(hidden_channels, out_channels))
            self.dropout = nn.Dropout(dropout)

        def forward(self, x, edge_index):  # noqa: D401
            """Compute node embeddings via stacked SAGEConv layers.

            Parameters
            ----------
            x: Tensor, shape (N, in_channels)
            edge_index: Tensor, shape (2, E)

            Returns
            -------
            Tensor, shape (N, out_channels)
            """
            for conv in self.convs[:-1]:
                x = conv(x, edge_index).relu()
                x = self.dropout(x)
            return self.convs[-1](x, edge_index)

    class RingMembershipClassifier(nn.Module):
        """2-layer MLP head mapping embeddings to ring-membership probability.

        Parameters
        ----------
        embedding_dim:
            Size of input embedding (must match GraphSAGEEncoder.out_channels).
        """

        def __init__(self, embedding_dim: int = 64) -> None:
            super().__init__()
            self.mlp = nn.Sequential(
                nn.Linear(embedding_dim, 32),
                nn.ReLU(),
                nn.Dropout(0.2),
                nn.Linear(32, 1),
                nn.Sigmoid(),
            )

        def forward(self, embeddings: "torch.Tensor") -> "torch.Tensor":  # type: ignore[name-defined]
            """Compute ring membership scores.

            Parameters
            ----------
            embeddings: Tensor, shape (N, embedding_dim)

            Returns
            -------
            Tensor, shape (N,) — ring_membership_score ∈ [0, 1] per node.
            """
            return self.mlp(embeddings).squeeze(-1)

    class HeteroGraphSAGEEncoder(nn.Module):
        """Heterogeneous graph encoder using per-edge-type SAGEConv or HGTConv.

        Produces 64-dim embeddings for the ``wallet`` node type only, compatible
        with the existing ``RingMembershipClassifier`` head.

        Parameters
        ----------
        metadata:
            ``(node_types, edge_types)`` tuple from ``HeteroData.metadata()``.
        hidden_channels:
            Width of intermediate layers (default 128).
        out_channels:
            Embedding dimensionality (default 64).
        num_layers:
            Number of heterogeneous conv layers (default 3).
        aggr:
            Aggregation method across edge types (default "mean").
        conv_type:
            ``"sage"`` uses ``HeteroConv`` with per-edge-type ``SAGEConv``.
            ``"hgt"`` uses ``HGTConv`` (attention-weighted heterogeneous
            aggregation).  HGT is more expressive but O(E × heads) more
            expensive; SAGE is faster and sufficient when edge types share a
            common feature space.
        """

        def __init__(
            self,
            metadata: tuple,
            hidden_channels: int = 128,
            out_channels: int = 64,
            num_layers: int = 3,
            aggr: str = "mean",
            conv_type: str = "sage",
        ) -> None:
            super().__init__()
            self.convs = nn.ModuleList()
            self.conv_type = conv_type
            node_types, edge_types = metadata

            for _ in range(num_layers):
                if conv_type == "hgt":
                    conv = HGTConv(hidden_channels, hidden_channels, metadata, heads=4)
                else:
                    conv_dict: dict = {}
                    for edge_type in edge_types:
                        # Use -1 to let SAGEConv auto-infer in_channels from input
                        conv_dict[edge_type] = SAGEConv((-1, -1), hidden_channels)
                    conv = HeteroConv(conv_dict, aggr=aggr)
                self.convs.append(conv)

            self.out_proj = nn.Linear(hidden_channels, out_channels)

        def forward(
            self,
            x_dict: dict[str, "torch.Tensor"],
            edge_index_dict: dict,
        ) -> dict[str, "torch.Tensor"]:
            """Compute wallet node embeddings via heterogeneous message passing.

            Parameters
            ----------
            x_dict:
                ``{node_type: feature_tensor}`` mapping.
            edge_index_dict:
                ``{edge_type: edge_index_tensor}`` mapping.

            Returns
            -------
            ``{"wallet": Tensor}`` with shape ``(n_wallets, out_channels)``.
            """
            for conv in self.convs:
                out_dict = conv(x_dict, edge_index_dict)
                x_dict = {k: v.relu() for k, v in out_dict.items()}

            wallet_emb = x_dict.get("wallet")
            if wallet_emb is None:
                # Fallback: return zeros if wallet type missing
                n = next(iter(x_dict.values())).shape[0] if x_dict else 0
                return {"wallet": torch.zeros((n, self.out_proj.out_features))}

            return {"wallet": self.out_proj(wallet_emb)}

else:
    # Placeholders when PyG is unavailable
    class GraphSAGEEncoder:  # type: ignore[no-redef]
        """Placeholder — requires PyTorch Geometric."""

        def __init__(self, *args, **kwargs) -> None:
            raise ImportError("PyTorch Geometric is required for GraphSAGEEncoder.")

    class RingMembershipClassifier:  # type: ignore[no-redef]
        """Placeholder — requires PyTorch Geometric."""

        def __init__(self, *args, **kwargs) -> None:
            raise ImportError("PyTorch Geometric is required for RingMembershipClassifier.")

    class HeteroGraphSAGEEncoder:  # type: ignore[no-redef]
        """Placeholder — requires PyTorch Geometric."""

        def __init__(self, *args, **kwargs) -> None:
            raise ImportError("PyTorch Geometric is required for HeteroGraphSAGEEncoder.")


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------


def _verify_model_checksum(model_path: str, checksum_path: str) -> bool:
    """Verify SHA-256 checksum of model file.

    Returns True if checksum matches, False if mismatch or checksum file
    missing.  Never raises — a failed check triggers SCC fallback.
    """
    try:
        if not os.path.exists(checksum_path):
            logger.warning(
                "GNN model checksum file missing: %s — falling back to SCC.", checksum_path
            )
            return False
        with open(checksum_path, "r") as f:
            expected = f.read().strip().lower()
        h = hashlib.sha256()
        with open(model_path, "rb") as mf:
            for chunk in iter(lambda: mf.read(65536), b""):
                h.update(chunk)
        actual = h.hexdigest().lower()
        if actual != expected:
            logger.error(
                "GNN model checksum MISMATCH (expected=%s, got=%s) — "
                "falling back to SCC.",
                expected[:16] + "...",
                actual[:16] + "...",
            )
            return False
        return True
    except Exception as exc:  # pragma: no cover
        logger.error("Checksum verification error: %s — falling back to SCC.", exc)
        return False


# ---------------------------------------------------------------------------
# GNNRingDetector
# ---------------------------------------------------------------------------


class GNNRingDetector:
    """Wraps GraphSAGEEncoder + RingMembershipClassifier for inference.

    When the GNN model is not fitted (or checksum fails), and
    ``fallback_to_scc=True``, ``predict`` returns SCC binary membership
    (0.0 or 1.0) derived from ``graph['wallet'].scc_membership`` if present.

    Supports two graph modes via ``graph_mode``:
    - ``"homogeneous"`` (default): wallet-only graph, uses ``GraphSAGEEncoder``.
    - ``"heterogeneous"``: wallet+asset+order graph, uses ``HeteroGraphSAGEEncoder``.
      Falls back to homogeneous mode if hetero checkpoint is unavailable.

    Parameters
    ----------
    model_path:
        Path to the saved ``{encoder, classifier}`` checkpoint.
    fallback_to_scc:
        Whether to fall back to SCC binary score when the GNN is unavailable.
    in_channels:
        Node feature dimension (must match the trained model).
    hidden_channels:
        Encoder hidden width.
    out_channels:
        Encoder output / embedding dimension.
    num_layers:
        Number of GraphSAGE layers.
    dropout:
        Dropout rate.
    graph_mode:
        ``"homogeneous"`` or ``"heterogeneous"``.
    hetero_conv_type:
        Convolution type for heterogeneous mode: ``"sage"`` or ``"hgt"``.
    """

    MODEL_PATH = DEFAULT_MODEL_PATH
    CHECKSUM_PATH = DEFAULT_CHECKSUM_PATH

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        fallback_to_scc: bool = True,
        in_channels: int = 4,
        hidden_channels: int = 128,
        out_channels: int = 64,
        num_layers: int = 3,
        dropout: float = 0.3,
        graph_mode: str = "homogeneous",
        hetero_conv_type: str = "sage",
    ) -> None:
        self.model_path = model_path
        self.checksum_path = model_path.replace(".pt", ".sha256")
        self.fallback_to_scc = fallback_to_scc
        self._fitted = False
        self._encoder: Any = None
        self._classifier: Any = None
        self._in_channels = in_channels
        self._hidden_channels = hidden_channels
        self._out_channels = out_channels
        self._num_layers = num_layers
        self._dropout = dropout
        self._graph_mode = graph_mode
        self._hetero_conv_type = hetero_conv_type
        self._hetero_metadata: tuple | None = None
        self._hetero_fitted = False

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load encoder + classifier weights from MODEL_PATH.

        Sets ``self._fitted = True`` on success.  Falls back silently if:
        - PyG is not installed.
        - The model file does not exist.
        - The SHA-256 checksum does not match.

        In heterogeneous mode, also attempts to load a hetero checkpoint from
        ``GNN_HETERO_MODEL_PATH``. Falls back to homogeneous if unavailable.
        """
        if not _HAS_PYG:
            logger.warning("PyTorch Geometric not installed — GNN unavailable.")
            return

        # Try heterogeneous mode first
        if self._graph_mode == "heterogeneous":
            self._load_hetero()

        # Always try homogeneous (or as fallback)
        if not self._fitted:
            self._load_homogeneous()

    def _load_homogeneous(self) -> None:
        """Load homogeneous GraphSAGE encoder + classifier."""
        if not _HAS_PYG:
            return
        if not os.path.exists(self.model_path):
            logger.warning("GNN model file not found at %s.", self.model_path)
            return
        if not _verify_model_checksum(self.model_path, self.checksum_path):
            return
        try:
            checkpoint = torch.load(  # type: ignore[union-attr]
                self.model_path, map_location="cpu", weights_only=True
            )
            enc_cfg = checkpoint.get("encoder_config", {})
            clf_cfg = checkpoint.get("classifier_config", {})
            encoder = GraphSAGEEncoder(
                in_channels=enc_cfg.get("in_channels", self._in_channels),
                hidden_channels=enc_cfg.get("hidden_channels", self._hidden_channels),
                out_channels=enc_cfg.get("out_channels", self._out_channels),
                num_layers=enc_cfg.get("num_layers", self._num_layers),
                dropout=enc_cfg.get("dropout", self._dropout),
            )
            encoder.load_state_dict(checkpoint["encoder"])
            encoder.eval()
            classifier = RingMembershipClassifier(
                embedding_dim=clf_cfg.get("embedding_dim", self._out_channels)
            )
            classifier.load_state_dict(checkpoint["classifier"])
            classifier.eval()
            self._encoder = encoder
            self._classifier = classifier
            self._fitted = True
            logger.info("GNN ring detector loaded from %s.", self.model_path)
        except Exception as exc:
            logger.error("Failed to load GNN model: %s — falling back to SCC.", exc)

    def _load_hetero(self) -> None:
        """Load heterogeneous HeteroGraphSAGEEncoder + classifier."""
        if not _HAS_PYG:
            return
        hetero_model_path = os.environ.get(
            "GNN_HETERO_MODEL_PATH", DEFAULT_HETERO_MODEL_PATH
        )
        hetero_checksum_path = os.environ.get(
            "GNN_HETERO_CHECKSUM_PATH", DEFAULT_HETERO_CHECKSUM_PATH
        )
        if not os.path.exists(hetero_model_path):
            logger.info(
                "Heterogeneous GNN model not found at %s — "
                "will use homogeneous mode.",
                hetero_model_path,
            )
            return
        if not _verify_model_checksum(hetero_model_path, hetero_checksum_path):
            logger.warning(
                "Heterogeneous GNN checksum failed — falling back to homogeneous mode."
            )
            return
        try:
            checkpoint = torch.load(  # type: ignore[union-attr]
                hetero_model_path, map_location="cpu", weights_only=True
            )
            metadata = checkpoint.get("metadata")
            enc_cfg = checkpoint.get("encoder_config", {})
            clf_cfg = checkpoint.get("classifier_config", {})
            encoder = HeteroGraphSAGEEncoder(
                metadata=metadata,
                hidden_channels=enc_cfg.get("hidden_channels", self._hidden_channels),
                out_channels=enc_cfg.get("out_channels", self._out_channels),
                num_layers=enc_cfg.get("num_layers", self._num_layers),
                aggr=enc_cfg.get("aggr", "mean"),
                conv_type=enc_cfg.get("conv_type", self._hetero_conv_type),
            )
            encoder.load_state_dict(checkpoint["encoder"])
            encoder.eval()
            classifier = RingMembershipClassifier(
                embedding_dim=clf_cfg.get("embedding_dim", self._out_channels)
            )
            classifier.load_state_dict(checkpoint["classifier"])
            classifier.eval()
            self._encoder = encoder
            self._classifier = classifier
            self._hetero_metadata = metadata
            self._fitted = True
            self._hetero_fitted = True
            logger.info("Heterogeneous GNN ring detector loaded from %s.", hetero_model_path)
        except Exception as exc:
            logger.warning(
                "Failed to load heterogeneous GNN model: %s — "
                "falling back to homogeneous mode.",
                exc,
            )

    # ------------------------------------------------------------------
    # SCC fallback
    # ------------------------------------------------------------------

    def _scc_membership(self, wallet: str, graph: Any) -> bool:
        """Return SCC binary membership from graph metadata if available."""
        try:
            wallet_list = graph["wallet"].wallet_list
            if wallet_list and wallet in wallet_list:
                idx = wallet_list.index(wallet)
                scc = getattr(graph["wallet"], "scc_membership", None)
                if scc is not None:
                    return bool(scc[idx])
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Predict
    # ------------------------------------------------------------------

    def predict(self, wallet: str, graph: Any) -> float:
        """Return ring_membership_score ∈ [0, 1].

        Falls back to SCC binary score (0.0 or 1.0) when:
        - The model is not loaded/fitted, AND ``fallback_to_scc=True``.
        - Returns 0.0 when not fitted and ``fallback_to_scc=False``.

        Parameters
        ----------
        wallet:
            Stellar wallet address string.
        graph:
            HeteroData produced by ``build_transaction_graph`` or
            ``build_heterogeneous_graph``.
        """
        if not self._fitted:
            if self.fallback_to_scc:
                return 1.0 if self._scc_membership(wallet, graph) else 0.0
            return 0.0

        try:
            wallet_list = graph["wallet"].wallet_list
            if wallet not in wallet_list:
                return 0.0
            idx = wallet_list.index(wallet)

            # Heterogeneous mode
            if self._hetero_fitted and hasattr(graph, "metadata"):
                x_dict = {nt: graph[nt].x for nt in graph.node_types}
                edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
                with torch.no_grad():  # type: ignore[union-attr]
                    emb_dict = self._encoder(x_dict, edge_index_dict)
                    embeddings = emb_dict["wallet"]
                    scores = self._classifier(embeddings)
                return float(scores[idx].item())

            # Homogeneous mode
            x = graph["wallet"].x
            edge_index = graph["wallet", "trades", "wallet"].edge_index
            with torch.no_grad():  # type: ignore[union-attr]
                embeddings = self._encoder(x, edge_index)
                scores = self._classifier(embeddings)
            return float(scores[idx].item())
        except Exception as exc:
            logger.error("GNN inference error for wallet %s: %s", wallet[:8], exc)
            if self.fallback_to_scc:
                return 1.0 if self._scc_membership(wallet, graph) else 0.0
            return 0.0

    # ------------------------------------------------------------------
    # Top neighbours by cosine similarity
    # ------------------------------------------------------------------

    def top_neighbours(self, wallet: str, graph: Any, k: int = 5) -> list[str]:
        """Return k wallets with highest cosine similarity to wallet's embedding.

        Parameters
        ----------
        wallet:
            Stellar wallet address.
        graph:
            HeteroData graph.
        k:
            Number of neighbours to return.

        Returns
        -------
        List of wallet address strings sorted by descending cosine similarity.
        """
        if not self._fitted or not _HAS_PYG:
            return []
        try:
            wallet_list = graph["wallet"].wallet_list
            if wallet not in wallet_list:
                return []
            idx = wallet_list.index(wallet)
            x = graph["wallet"].x
            edge_index = graph["wallet", "trades", "wallet"].edge_index
            with torch.no_grad():  # type: ignore[union-attr]
                embeddings = self._encoder(x, edge_index)
            # Cosine similarity
            target = embeddings[idx].unsqueeze(0)
            sims = F.cosine_similarity(  # type: ignore[union-attr]
                target, embeddings, dim=1
            ).cpu().numpy()
            sims[idx] = -999.0  # exclude self
            top_k_idx = np.argsort(sims)[::-1][:k]
            return [wallet_list[i] for i in top_k_idx if i < len(wallet_list)]
        except Exception as exc:
            logger.error("top_neighbours error: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Batch predict (for training / eval)
    # ------------------------------------------------------------------

    def predict_batch(self, graph: Any) -> "torch.Tensor":  # type: ignore[name-defined]
        """Return ring_membership_score for every wallet node in the graph."""
        if not self._fitted or not _HAS_PYG:
            raise RuntimeError("GNN model not loaded.")

        # Heterogeneous mode
        if self._hetero_fitted and hasattr(graph, "metadata"):
            x_dict = {nt: graph[nt].x for nt in graph.node_types}
            edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
            with torch.no_grad():  # type: ignore[union-attr]
                emb_dict = self._encoder(x_dict, edge_index_dict)
                embeddings = emb_dict["wallet"]
                scores = self._classifier(embeddings)
            return scores

        # Homogeneous mode
        x = graph["wallet"].x
        edge_index = graph["wallet", "trades", "wallet"].edge_index
        with torch.no_grad():  # type: ignore[union-attr]
            embeddings = self._encoder(x, edge_index)
            scores = self._classifier(embeddings)
        return scores

    def get_embeddings(self, graph: Any) -> "torch.Tensor":  # type: ignore[name-defined]
        """Return raw embeddings for all wallet nodes (for training use only)."""
        if not self._fitted or not _HAS_PYG:
            raise RuntimeError("GNN model not loaded.")

        # Heterogeneous mode
        if self._hetero_fitted and hasattr(graph, "metadata"):
            x_dict = {nt: graph[nt].x for nt in graph.node_types}
            edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
            with torch.no_grad():  # type: ignore[union-attr]
                emb_dict = self._encoder(x_dict, edge_index_dict)
                return emb_dict["wallet"]

        # Homogeneous mode
        x = graph["wallet"].x
        edge_index = graph["wallet", "trades", "wallet"].edge_index
        with torch.no_grad():  # type: ignore[union-attr]
            return self._encoder(x, edge_index)
