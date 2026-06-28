"""Graph-based wash-ring discovery for SDEX trade flows."""

from __future__ import annotations

import logging
import os
import statistics
from typing import Any, Optional

import networkx as nx
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix, lil_matrix

logger = logging.getLogger(__name__)


def build_transaction_graph(trades: pd.DataFrame) -> nx.DiGraph:
    """Build a directed graph from a trades DataFrame.

    Nodes are Stellar account addresses. Edges are directed from base_account to
    counter_account and store aggregate volume, trade count, and trade times.
    Self-loops are preserved.
    """
    graph = nx.DiGraph()
    if trades.empty:
        return graph

    required_columns = {"base_account", "counter_account"}
    missing = required_columns - set(trades.columns)
    if missing:
        raise ValueError(f"trades is missing required columns: {sorted(missing)}")

    base_accounts = trades["base_account"].astype(str).to_numpy()
    counter_accounts = trades["counter_account"].astype(str).to_numpy()
    if "base_amount" in trades:
        amounts = pd.to_numeric(trades["base_amount"], errors="coerce").fillna(0.0).clip(lower=0).to_numpy()
    else:
        amounts = [0.0] * len(trades)
    if "ledger_close_time" in trades:
        timestamps = pd.to_datetime(trades["ledger_close_time"], utc=True, errors="coerce").to_numpy()
    else:
        timestamps = [pd.NaT] * len(trades)

    accounts = set(base_accounts) | set(counter_accounts)
    graph.add_nodes_from(sorted(account for account in accounts if account))

    edge_data: dict[tuple[str, str], list] = {}
    for base_account, counter_account, amount, timestamp in zip(
        base_accounts, counter_accounts, amounts, timestamps
    ):
        key = (base_account, counter_account)
        entry = edge_data.get(key)
        if entry is None:
            edge_data[key] = [float(amount), 1, []]
            entry = edge_data[key]
        else:
            entry[0] += float(amount)
            entry[1] += 1

        if not pd.isna(timestamp):
            entry[2].append(float(pd.Timestamp(timestamp).timestamp()))

    for (base_account, counter_account), (total_volume, trade_count, timestamps) in edge_data.items():
        graph.add_edge(
            base_account,
            counter_account,
            total_volume=float(total_volume),
            trade_count=int(trade_count),
            timestamps=timestamps,
        )

    return graph


def add_path_payment_edges(graph: nx.DiGraph, payments: pd.DataFrame) -> None:
    """Add directed account-to-account edges for each path payment operation.

    Unlike `build_transaction_graph` (which models direct SDEX counterparties),
    a path payment routes value from `source_account` to `destination_account`
    through one or more intermediary assets in a single atomic operation. A wash
    trader chains several such operations across colluding sub-accounts so the
    funds return to the originator without ever producing a direct wash-trade
    edge. Modelling each operation as a `source_account -> destination_account`
    edge lets the cycle search in `path_cycle_detector` recover those rings.

    `payments` is a DataFrame with the columns `source_account`,
    `destination_account`, `source_asset`, `destination_asset`,
    `source_amount`, `timestamp` (and optionally `transaction_hash`). Multiple
    payments between the same ordered pair accumulate on a single edge; each
    individual hop is retained under the edge's `path_payments` attribute so the
    cycle search can pick a time-ordered traversal.
    """
    if payments.empty:
        return

    required = {"source_account", "destination_account"}
    missing = required - set(payments.columns)
    if missing:
        raise ValueError(f"payments is missing required columns: {sorted(missing)}")

    has_tx_hash = "transaction_hash" in payments.columns
    for row in payments.itertuples(index=False):
        source = str(getattr(row, "source_account"))
        destination = str(getattr(row, "destination_account"))
        if not source or not destination:
            continue

        amount = float(getattr(row, "source_amount", 0.0) or 0.0)
        source_asset = str(getattr(row, "source_asset", "") or "")
        destination_asset = str(getattr(row, "destination_asset", "") or "")
        raw_ts = getattr(row, "timestamp", None)
        timestamp = pd.Timestamp(raw_ts) if raw_ts is not None and not pd.isna(raw_ts) else None
        tx_hash = str(getattr(row, "transaction_hash")) if has_tx_hash else None

        hop = {
            "amount_xlm": amount,
            "source_asset": source_asset,
            "destination_asset": destination_asset,
            "timestamp": timestamp,
            "transaction_hash": tx_hash,
        }

        if graph.has_edge(source, destination):
            edge = graph[source][destination]
            edge["total_volume"] = float(edge.get("total_volume", 0.0)) + amount
            edge["payment_count"] = int(edge.get("payment_count", 0)) + 1
            edge.setdefault("path_payments", []).append(hop)
        else:
            graph.add_edge(
                source,
                destination,
                total_volume=amount,
                payment_count=1,
                path_payments=[hop],
            )


def find_wash_rings(
    graph: nx.DiGraph,
    min_ring_size: int = 3,
    max_ring_size: int = 10,
    min_cycle_volume: float = 0.0,
) -> list[dict]:
    """Find candidate wash rings using Tarjan's SCC algorithm."""
    if min_ring_size < 1:
        raise ValueError("min_ring_size must be at least 1")
    if max_ring_size < min_ring_size:
        raise ValueError("max_ring_size must be greater than or equal to min_ring_size")

    rings: list[dict[str, Any]] = []
    for component in nx.strongly_connected_components(graph):
        if len(component) < min_ring_size:
            continue

        accounts = sorted(component)
        subgraph = graph.subgraph(accounts)
        total_volume = _component_total_volume(subgraph)
        timing_tightness = _timing_tightness(subgraph)
        avg_trade_count = _avg_trade_count(subgraph)

        if len(accounts) > max_ring_size:
            cycle_volume = total_volume * 0.5
            if cycle_volume < min_cycle_volume:
                continue
            logger.warning(
                "Detected truncated wash-ring SCC with %d accounts; cycle volume is approximate",
                len(accounts),
            )
            rings.append(
                {
                    "accounts": accounts,
                    "total_volume": total_volume,
                    "cycle_volume": cycle_volume,
                    "avg_trade_count": avg_trade_count,
                    "timing_tightness": timing_tightness,
                    "truncated": True,
                }
            )
            continue

        cycle_volume = _cycle_volume(subgraph, min_ring_size)
        if cycle_volume < min_cycle_volume:
            continue

        rings.append(
            {
                "accounts": accounts,
                "total_volume": total_volume,
                "cycle_volume": cycle_volume,
                "avg_trade_count": avg_trade_count,
                "timing_tightness": timing_tightness,
                "truncated": False,
            }
        )

    return sorted(rings, key=lambda ring: (ring["total_volume"], ring["cycle_volume"]), reverse=True)


def build_ring_membership_index(
    rings: list[dict],
    trades: pd.DataFrame | None = None,
    graph: nx.DiGraph | None = None,
) -> dict[str, dict]:
    """Return account -> metadata for the strongest detected ring per account."""
    membership: dict[str, dict] = {}
    for ring in rings:
        accounts = list(ring.get("accounts", []))
        if not accounts:
            continue

        ring_size = len(accounts)
        cycle_volume = float(ring.get("cycle_volume", 0.0))
        timing_tightness = float(ring.get("timing_tightness", 0.0))
        timing_tightness_score = 1.0 / (1.0 + timing_tightness)
        totals = _account_outgoing_volumes(accounts, trades=trades, graph=graph)

        for account in accounts:
            total_volume = float(totals.get(account, 0.0))
            cycle_volume_ratio = min(1.0, cycle_volume / total_volume) if total_volume > 0 else 0.0
            metadata = {
                "accounts": accounts,
                "ring_size": ring_size,
                "wash_ring_size": float(ring_size),
                "cycle_volume": cycle_volume,
                "cycle_volume_ratio": cycle_volume_ratio,
                "timing_tightness": timing_tightness,
                "timing_tightness_score": timing_tightness_score,
                "truncated": bool(ring.get("truncated", False)),
            }
            current = membership.get(account)
            if current is None or _ring_metadata_precedes(metadata, current):
                membership[account] = metadata

    return membership


def _component_total_volume(subgraph: nx.DiGraph) -> float:
    return float(
        sum(
            float(data.get("total_volume", 0.0))
            for _, _, data in subgraph.edges(data=True)
        )
    )


def _avg_trade_count(subgraph: nx.DiGraph) -> float:
    edges = list(subgraph.edges(data=True))
    if not edges:
        return 0.0
    return float(sum(float(data.get("trade_count", 0.0)) for _, _, data in edges) / len(edges))


def _timing_tightness(subgraph: nx.DiGraph) -> float:
    timestamps: list[float] = []
    for _, _, data in subgraph.edges(data=True):
        timestamps.extend(float(ts) for ts in data.get("timestamps", []) if ts is not None)

    timestamps = sorted(timestamps)
    if len(timestamps) < 2:
        return 0.0

    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    return float(statistics.pstdev(intervals))


def _cycle_volume(subgraph: nx.DiGraph, min_ring_size: int) -> float:
    best_cycle_volume = 0.0
    for cycle in nx.simple_cycles(subgraph, length_bound=subgraph.number_of_nodes()):
        if len(cycle) < min_ring_size:
            continue
        edge_volumes = [
            float(subgraph[cycle[i]][cycle[(i + 1) % len(cycle)]].get("total_volume", 0.0))
            for i in range(len(cycle))
        ]
        if not edge_volumes:
            continue
        best_cycle_volume = max(best_cycle_volume, min(edge_volumes))
    return float(best_cycle_volume)


def _account_outgoing_volumes(
    accounts: list[str],
    *,
    trades: pd.DataFrame | None,
    graph: nx.DiGraph | None,
) -> dict[str, float]:
    if trades is not None and not trades.empty and "base_account" in trades and "base_amount" in trades:
        tmp = trades[["base_account", "base_amount"]].copy()
        tmp["base_account"] = tmp["base_account"].astype(str)
        tmp["base_amount"] = pd.to_numeric(tmp["base_amount"], errors="coerce").fillna(0.0).clip(lower=0)
        totals = tmp[tmp["base_account"].isin(accounts)].groupby("base_account")["base_amount"].sum()
        return {account: float(totals.get(account, 0.0)) for account in accounts}

    if graph is not None:
        return {
            account: float(
                sum(
                    float(graph[account][successor].get("total_volume", 0.0))
                    for successor in graph.successors(account)
                )
            )
            for account in accounts
        }

    return {account: 0.0 for account in accounts}


def _ring_metadata_precedes(candidate: dict, current: dict) -> bool:
    if candidate["wash_ring_size"] != current["wash_ring_size"]:
        return candidate["wash_ring_size"] > current["wash_ring_size"]
    if candidate["timing_tightness_score"] != current["timing_tightness_score"]:
        return candidate["timing_tightness_score"] > current["timing_tightness_score"]
    return candidate["cycle_volume_ratio"] > current["cycle_volume_ratio"]


# ---------------------------------------------------------------------------
# Scalability layer: iterative Tarjan SCC + CSR adjacency + TradeGraph API
# ---------------------------------------------------------------------------

GRAPH_MMAP_THRESHOLD: int = int(os.getenv("GRAPH_MMAP_THRESHOLD", "50000"))
MAX_GRAPH_NODES: int = int(os.getenv("MAX_GRAPH_NODES", "1000000"))


class GraphTooLargeError(Exception):
    """Raised when a graph exceeds MAX_GRAPH_NODES nodes."""


class NodeIndex:
    """Bijective str↔int mapping for Stellar account identifiers.

    Provides O(1) lookup in both directions. Appending is amortised O(1).
    """

    def __init__(self) -> None:
        self._str_to_int: dict[str, int] = {}
        self._int_to_str: list[str] = []

    def add(self, node: str) -> int:
        """Return the integer index for *node*, creating it if absent."""
        if node not in self._str_to_int:
            idx = len(self._int_to_str)
            self._str_to_int[node] = idx
            self._int_to_str.append(node)
        return self._str_to_int[node]

    def get_id(self, node: str) -> Optional[int]:
        """Return the integer index for *node*, or ``None`` if unknown."""
        return self._str_to_int.get(node)

    def get_node(self, idx: int) -> str:
        """Return the string identifier for integer index *idx*."""
        return self._int_to_str[idx]

    def __len__(self) -> int:
        return len(self._int_to_str)


class IterativeTarjanSCC:
    """Tarjan's SCC algorithm using an explicit work-stack.

    Eliminates Python's recursion limit for arbitrarily large directed graphs.
    Self-loops are skipped in the adjacency traversal; they never form multi-node
    SCCs and cannot cause infinite loops.
    """

    def run(self, graph: dict[int, list[int]]) -> list[list[int]]:
        """Return all SCCs of *graph*; each SCC is a list of integer node indices.

        *graph* is an adjacency dict ``{node_id: [neighbour_id, ...]}``.
        Nodes that appear only as targets (no outgoing edges) are not required
        to have an explicit key; they are still discovered as singleton SCCs
        when reached via an edge from another node.
        """
        index_counter = [0]
        stack: list[int] = []
        lowlink: dict[int, int] = {}
        index: dict[int, int] = {}
        on_stack: set[int] = set()
        sccs: list[list[int]] = []

        def strongconnect(v: int) -> None:
            work_stack: list[tuple[int, Any]] = [(v, iter(graph.get(v, [])))]
            index[v] = lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            while work_stack:
                v, neighbours = work_stack[-1]
                try:
                    w = next(neighbours)
                    if w == v:
                        continue
                    if w not in index:
                        index[w] = lowlink[w] = index_counter[0]
                        index_counter[0] += 1
                        stack.append(w)
                        on_stack.add(w)
                        work_stack.append((w, iter(graph.get(w, []))))
                    elif w in on_stack:
                        lowlink[v] = min(lowlink[v], index[w])
                except StopIteration:
                    work_stack.pop()
                    if work_stack:
                        parent = work_stack[-1][0]
                        lowlink[parent] = min(lowlink[parent], lowlink[v])
                    if lowlink[v] == index[v]:
                        scc: list[int] = []
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            scc.append(w)
                            if w == v:
                                break
                        sccs.append(scc)

        for v in list(graph.keys()):
            if v not in index:
                strongconnect(v)

        return sccs


class SparseTradeGraph:
    """CSR-backed adjacency for trade graphs with many nodes.

    Stores edges in a scipy ``csr_matrix`` (Compressed Sparse Row) which
    provides contiguous memory layout and fast row-slice access for the
    Tarjan adjacency traversal.  Node identifiers are mapped to integer
    indices via a shared :class:`NodeIndex`.
    """

    def __init__(self, node_index: NodeIndex) -> None:
        self._node_index = node_index
        self._adj: Optional[csr_matrix] = None

    def build_from_trades(self, trades: list) -> None:
        """Populate the CSR adjacency from a list of Trade records.

        All nodes referenced in *trades* are registered in the shared
        :class:`NodeIndex`.  Raises :class:`GraphTooLargeError` when the
        resulting node count exceeds ``MAX_GRAPH_NODES``.
        """
        # Two-pass: first register all nodes so the matrix size is final.
        for trade in trades:
            counter = getattr(trade, "counter_account", None)
            if not counter:
                continue
            self._node_index.add(trade.base_account)
            self._node_index.add(counter)

        n = len(self._node_index)
        if n > MAX_GRAPH_NODES:
            raise GraphTooLargeError(
                f"Graph has {n} nodes which exceeds MAX_GRAPH_NODES={MAX_GRAPH_NODES}"
            )
        logger.info("Building CSR adjacency for %d nodes from %d trades", n, len(trades))
        lil = lil_matrix((n, n), dtype=np.float64)
        for trade in trades:
            counter = getattr(trade, "counter_account", None)
            if not counter:
                continue
            i = self._node_index.get_id(trade.base_account)
            j = self._node_index.get_id(counter)
            if i is not None and j is not None:
                lil[i, j] += float(getattr(trade, "base_amount", 0) or 0)
        self._adj = csr_matrix(lil)

    def _load_edge_data(self, edge_data: dict[tuple[str, str], list]) -> None:
        """Internal: build CSR from pre-aggregated ``{(base, counter): [vol, …]}``."""
        n = len(self._node_index)
        if n > MAX_GRAPH_NODES:
            raise GraphTooLargeError(
                f"Graph has {n} nodes which exceeds MAX_GRAPH_NODES={MAX_GRAPH_NODES}"
            )
        logger.info("Building CSR adjacency for %d nodes from aggregated edge data", n)
        lil = lil_matrix((n, n), dtype=np.float64)
        for (base, counter), (vol, _count, _ts) in edge_data.items():
            i = self._node_index.get_id(base)
            j = self._node_index.get_id(counter)
            if i is not None and j is not None:
                lil[i, j] = float(vol)
        self._adj = csr_matrix(lil)

    def to_adjacency_dict(self) -> dict[int, list[int]]:
        """Convert the CSR matrix to an adjacency dict for :class:`IterativeTarjanSCC`."""
        if self._adj is None:
            return {}
        cx = self._adj.tocsr()
        adj: dict[int, list[int]] = {}
        for i in range(cx.shape[0]):
            start, end = cx.indptr[i], cx.indptr[i + 1]
            if end > start:
                adj[i] = cx.indices[start:end].tolist()
        return adj


class TradeGraph:
    """Incrementally-built directed trade graph with public wash-ring API.

    Callers add trades one at a time via :meth:`add_trade`; the internal
    adjacency representation is chosen automatically:

    * ``n < GRAPH_MMAP_THRESHOLD`` — plain Python dict for adjacency.
    * ``n >= GRAPH_MMAP_THRESHOLD`` — :class:`SparseTradeGraph` (CSR matrix).

    SCC computation always uses the iterative :class:`IterativeTarjanSCC`;
    there is no recursion limit regardless of graph size.
    """

    def __init__(self) -> None:
        self._node_index = NodeIndex()
        # (base, counter) -> [total_volume, trade_count, [unix_timestamps]]
        self._edge_data: dict[tuple[str, str], list] = {}
        self._rings_cache: tuple | None = None  # (cache_key, rings_list)

    def add_trade(self, trade: Any) -> None:
        """Register a single trade edge in the graph.

        *trade* must expose ``base_account``, ``counter_account``,
        ``base_amount``, and ``ledger_close_time`` attributes (compatible with
        :class:`ingestion.data_models.Trade`).  Liquidity-pool trades where
        ``counter_account`` is ``None`` are silently skipped.

        Raises :class:`GraphTooLargeError` when adding this trade would push
        the node count above ``MAX_GRAPH_NODES``.
        """
        base = getattr(trade, "base_account", None)
        counter = getattr(trade, "counter_account", None)
        if not base or not counter:
            return

        self._node_index.add(base)
        self._node_index.add(counter)

        n = len(self._node_index)
        if n > MAX_GRAPH_NODES:
            raise GraphTooLargeError(
                f"Graph has {n} nodes which exceeds MAX_GRAPH_NODES={MAX_GRAPH_NODES}"
            )

        key = (base, counter)
        amount = float(getattr(trade, "base_amount", 0) or 0)
        raw_ts = getattr(trade, "ledger_close_time", None)

        if key not in self._edge_data:
            self._edge_data[key] = [0.0, 0, []]
        entry = self._edge_data[key]
        entry[0] += amount
        entry[1] += 1
        if raw_ts is not None:
            try:
                entry[2].append(float(pd.Timestamp(raw_ts).timestamp()))
            except Exception:
                pass

        self._rings_cache = None

    def _build_int_adjacency(self) -> dict[int, list[int]]:
        n = len(self._node_index)
        if n >= GRAPH_MMAP_THRESHOLD:
            sparse = SparseTradeGraph(self._node_index)
            sparse._load_edge_data(self._edge_data)
            return sparse.to_adjacency_dict()

        # Small-graph path: build directly from edge_data.
        adj: dict[int, list[int]] = {}
        seen: set[tuple[int, int]] = set()
        for base, counter in self._edge_data:
            i = self._node_index.get_id(base)
            j = self._node_index.get_id(counter)
            if i is None or j is None or (i, j) in seen:
                continue
            seen.add((i, j))
            adj.setdefault(i, []).append(j)
        return adj

    def _scc_edge_metrics(self, account_set: set[str]) -> tuple[float, float, float]:
        """Compute (total_volume, timing_tightness, avg_trade_count) in one pass.

        Avoids building a networkx graph for large SCCs where only these aggregate
        metrics are needed (cycle enumeration is skipped for truncated rings).
        """
        total_vol = 0.0
        all_timestamps: list[float] = []
        edge_count = 0
        total_trade_count = 0
        for (base, counter), (vol, cnt, timestamps) in self._edge_data.items():
            if base in account_set and counter in account_set:
                total_vol += vol
                total_trade_count += cnt
                edge_count += 1
                all_timestamps.extend(timestamps)

        tss = sorted(all_timestamps)
        if len(tss) >= 2:
            intervals = [b - a for a, b in zip(tss, tss[1:])]
            timing_tightness = float(statistics.pstdev(intervals))
        else:
            timing_tightness = 0.0
        avg_tc = total_trade_count / edge_count if edge_count else 0.0
        return total_vol, timing_tightness, avg_tc

    def _scc_small_subgraph(self, accounts: list[str]) -> nx.DiGraph:
        """Build nx.DiGraph for a *small* SCC (len <= max_ring_size) for cycle search."""
        account_set = set(accounts)
        subgraph: nx.DiGraph = nx.DiGraph()
        subgraph.add_nodes_from(accounts)
        for (base, counter), (vol, count, timestamps) in self._edge_data.items():
            if base in account_set and counter in account_set:
                subgraph.add_edge(
                    base,
                    counter,
                    total_volume=vol,
                    trade_count=count,
                    timestamps=list(timestamps),
                )
        return subgraph

    def find_wash_rings(
        self,
        min_ring_size: int = 3,
        max_ring_size: int = 10,
        min_cycle_volume: float = 0.0,
    ) -> list[dict]:
        """Find candidate wash rings using iterative Tarjan SCC.

        Returns the same structure as the module-level :func:`find_wash_rings`.
        Results are cached until the next :meth:`add_trade` call.
        """
        if min_ring_size < 1:
            raise ValueError("min_ring_size must be at least 1")
        if max_ring_size < min_ring_size:
            raise ValueError("max_ring_size must be greater than or equal to min_ring_size")

        cache_key = (min_ring_size, max_ring_size, min_cycle_volume)
        if self._rings_cache is not None and self._rings_cache[0] == cache_key:
            return self._rings_cache[1]

        int_adj = self._build_int_adjacency()
        raw_sccs = IterativeTarjanSCC().run(int_adj)

        rings: list[dict[str, Any]] = []
        for scc in raw_sccs:
            if len(scc) < min_ring_size:
                continue

            accounts = sorted(self._node_index.get_node(i) for i in scc)
            account_set = set(accounts)
            total_volume, timing_tightness, avg_trade_count = self._scc_edge_metrics(account_set)

            if len(accounts) > max_ring_size:
                cycle_volume = total_volume * 0.5
                if cycle_volume < min_cycle_volume:
                    continue
                logger.warning(
                    "Detected truncated wash-ring SCC with %d accounts; "
                    "cycle volume is approximate",
                    len(accounts),
                )
                rings.append(
                    {
                        "accounts": accounts,
                        "total_volume": total_volume,
                        "cycle_volume": cycle_volume,
                        "avg_trade_count": avg_trade_count,
                        "timing_tightness": timing_tightness,
                        "truncated": True,
                    }
                )
                continue

            # Small SCC: need networkx for exact cycle-volume enumeration.
            subgraph = self._scc_small_subgraph(accounts)
            cycle_volume = _cycle_volume(subgraph, min_ring_size)
            if cycle_volume < min_cycle_volume:
                continue

            rings.append(
                {
                    "accounts": accounts,
                    "total_volume": total_volume,
                    "cycle_volume": cycle_volume,
                    "avg_trade_count": avg_trade_count,
                    "timing_tightness": timing_tightness,
                    "truncated": False,
                }
            )

        result = sorted(
            rings,
            key=lambda r: (r["total_volume"], r["cycle_volume"]),
            reverse=True,
        )
        self._rings_cache = (cache_key, result)
        return result

    def get_ring_members(self, wallet: str) -> dict | None:
        """Return ring-membership metadata for *wallet*, or ``None``.

        Equivalent to ``build_ring_membership_index(rings)[wallet]`` but
        computed without building a full membership dict for every account.
        """
        rings = self.find_wash_rings()
        best: dict | None = None

        for ring in rings:
            accounts: list[str] = ring.get("accounts", [])
            if wallet not in accounts:
                continue

            ring_size = len(accounts)
            cycle_volume = float(ring.get("cycle_volume", 0.0))
            timing_tightness = float(ring.get("timing_tightness", 0.0))
            timing_tightness_score = 1.0 / (1.0 + timing_tightness)

            outgoing = sum(
                vol
                for (base, _counter), (vol, _cnt, _ts) in self._edge_data.items()
                if base == wallet
            )
            cycle_vol_ratio = min(1.0, cycle_volume / outgoing) if outgoing > 0 else 0.0

            metadata: dict = {
                "accounts": accounts,
                "ring_size": ring_size,
                "wash_ring_size": float(ring_size),
                "cycle_volume": cycle_volume,
                "cycle_volume_ratio": cycle_vol_ratio,
                "timing_tightness": timing_tightness,
                "timing_tightness_score": timing_tightness_score,
                "truncated": bool(ring.get("truncated", False)),
            }
            if best is None or _ring_metadata_precedes(metadata, best):
                best = metadata

        return best
