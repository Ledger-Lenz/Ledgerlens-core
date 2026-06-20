"""Builds temporal trade graphs from Trade records for the T-GNN layer."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np

try:
    import torch
    from torch_geometric.data import HeteroData
    _HAS_PYG = True
except ImportError:
    torch = None
    HeteroData = object
    _HAS_PYG = False

from ingestion.data_models import Trade

logger = logging.getLogger(__name__)

MAX_NODE_DEGREE = 10_000
DEFAULT_BUCKET_HOURS = 4


def _hash_wallet(address: str) -> str:
    """Returns a short, non-reversible hash of a wallet address for logging."""
    return hashlib.sha256(address.encode()).hexdigest()[:10]


@dataclass
class WalletNodeFeatures:
    """Per-wallet node feature bundle used to build the node feature matrix."""
    address: str
    account_age_days: float = 0.0
    total_volume: float = 0.0
    benford_mad: float = 0.0
    funding_source_cluster_id: int = -1


@dataclass
class GraphSnapshot:
    """A single temporal slice of the trade graph."""
    start: datetime
    end: datetime
    wallet_index: dict
    node_features: np.ndarray
    edge_index: np.ndarray
    edge_attr: np.ndarray


class TemporalGraphBuilder:
    """Builds time-sliced trade graphs from a stream of Trade records."""

    def __init__(self, bucket_hours: int = DEFAULT_BUCKET_HOURS,
                 max_node_degree: int = MAX_NODE_DEGREE) -> None:
        self.bucket_hours = bucket_hours
        self.max_node_degree = max_node_degree

    def build_snapshots(self, trades: Iterable[Trade], lookback_days: int,
                         node_feature_lookup: dict = None,
                         end_time: datetime = None) -> list:
        """Builds non-overlapping temporal graph snapshots covering the window."""
        node_feature_lookup = node_feature_lookup or {}
        end_time = end_time or datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=lookback_days)
        bucket = timedelta(hours=self.bucket_hours)

        trades = sorted(trades, key=lambda t: t.ledger_close_time)
        wallet_index = {}

        snapshots = []
        slice_start = start_time
        trade_cursor = 0
        n_trades = len(trades)

        while slice_start < end_time:
            slice_end = min(slice_start + bucket, end_time)

            edges = []
            while trade_cursor < n_trades and trades[trade_cursor].ledger_close_time < slice_end:
                t = trades[trade_cursor]
                trade_cursor += 1
                if t.ledger_close_time < slice_start:
                    continue
                if t.base_account == t.counter_account:
                    continue
                edges.append((
                    t.base_account,
                    t.counter_account,
                    [
                        float(t.base_amount),
                        float(getattr(t, "price", 0.0) or 0.0),
                        1.0 if getattr(t, "trade_type", "orderbook") == "liquidity_pool" else 0.0,
                    ],
                ))

            for src, dst, _ in edges:
                for addr in (src, dst):
                    if addr not in wallet_index:
                        wallet_index[addr] = len(wallet_index)

            capped_edges = self._cap_high_degree(edges)
            snapshot = self._materialize_snapshot(
                slice_start, slice_end, wallet_index, capped_edges, node_feature_lookup
            )
            snapshots.append(snapshot)
            slice_start = slice_end

        logger.debug(
            "Built %d graph snapshots covering %d wallets (hashed: %s)",
            len(snapshots), len(wallet_index),
            [_hash_wallet(a) for a in list(wallet_index)[:3]],
        )
        return snapshots

    def _cap_high_degree(self, edges):
        """Caps per-wallet degree to max_node_degree, keeping highest-volume edges."""
        degree = {}
        for src, dst, _ in edges:
            degree[src] = degree.get(src, 0) + 1
            degree[dst] = degree.get(dst, 0) + 1

        over_cap = {w for w, d in degree.items() if d > self.max_node_degree}
        if not over_cap:
            return edges

        kept = []
        per_wallet_kept = {}
        for src, dst, attrs in sorted(edges, key=lambda e: e[2][0], reverse=True):
            touches_capped = src in over_cap or dst in over_cap
            if not touches_capped:
                kept.append((src, dst, attrs))
                continue
            ok = True
            for w in (src, dst):
                if w in over_cap and per_wallet_kept.get(w, 0) >= self.max_node_degree:
                    ok = False
            if ok:
                kept.append((src, dst, attrs))
                for w in (src, dst):
                    if w in over_cap:
                        per_wallet_kept[w] = per_wallet_kept.get(w, 0) + 1
        return kept

    def _materialize_snapshot(self, start, end, wallet_index, edges, node_feature_lookup):
        """Assembles numpy arrays for a single GraphSnapshot."""
        n = len(wallet_index)
        node_features = np.zeros((n, 4), dtype=np.float32)
        for addr, idx in wallet_index.items():
            feats = node_feature_lookup.get(addr)
            if feats is not None:
                node_features[idx] = [
                    feats.account_age_days,
                    feats.total_volume,
                    feats.benford_mad,
                    float(feats.funding_source_cluster_id),
                ]

        if edges:
            edge_index = np.array(
                [[wallet_index[s], wallet_index[d]] for s, d, _ in edges], dtype=np.int64
            ).T
            edge_attr = np.array([a for _, _, a in edges], dtype=np.float32)
        else:
            edge_index = np.zeros((2, 0), dtype=np.int64)
            edge_attr = np.zeros((0, 3), dtype=np.float32)

        return GraphSnapshot(
            start=start, end=end, wallet_index=dict(wallet_index),
            node_features=node_features, edge_index=edge_index, edge_attr=edge_attr,
        )


def snapshot_to_hetero_data(snapshot: GraphSnapshot):
    """Converts a GraphSnapshot into a PyTorch Geometric HeteroData object."""
    if not _HAS_PYG:
        raise RuntimeError(
            "torch_geometric is required for snapshot_to_hetero_data(); "
            "install it via `pip install torch torch_geometric`."
        )
    data = HeteroData()
    data["wallet"].x = torch.tensor(snapshot.node_features, dtype=torch.float32)
    data["wallet", "trade", "wallet"].edge_index = torch.tensor(snapshot.edge_index, dtype=torch.long)
    data["wallet", "trade", "wallet"].edge_attr = torch.tensor(snapshot.edge_attr, dtype=torch.float32)
    return data
