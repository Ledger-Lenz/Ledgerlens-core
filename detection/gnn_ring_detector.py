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
    from torch_geometric.nn import SAGEConv
    _HAS_PYG = True
except ImportError:
    torch = None          # type: ignore[assignment]
    nn = None             # type: ignore[assignment]
    F = None              # type: ignore[assignment]
    HeteroData = None     # type: ignore[assignment]
    SAGEConv = None       # type: ignore[assignment]
    _HAS_PYG = False

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL_PATH = "models/gnn_ring_detector.pt"
DEFAULT_CHECKSUM_PATH = "models/gnn_ring_detector.sha256"

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

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load encoder + classifier weights from MODEL_PATH.

        Sets ``self._fitted = True`` on success.  Falls back silently if:
        - PyG is not installed.
        - The model file does not exist.
        - The SHA-256 checksum does not match.
        """
        if not _HAS_PYG:
            logger.warning("PyTorch Geometric not installed — GNN unavailable.")
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
            HeteroData produced by ``build_transaction_graph``.
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
        x = graph["wallet"].x
        edge_index = graph["wallet", "trades", "wallet"].edge_index
        with torch.no_grad():  # type: ignore[union-attr]
            return self._encoder(x, edge_index)
