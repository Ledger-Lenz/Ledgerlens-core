"""Adaptive sharded graph engine partitioner using community detection.

Rebalancing
-----------
When shard-holding workers are added or removed the shard count changes and
:meth:`GraphShardPartitioner.rebalance` computes a migration plan from the
previous :class:`ShardAssignment` to a new one:

1. The graph is re-partitioned with the new shard count.
2. New shard ids are relabelled to maximise overlap with the previous
   assignment (greedy, largest overlap first), so nodes whose community did
   not change keep their shard and are not moved.
3. Every node whose shard differs becomes a single ``(source, target)`` move.
   Nodes that were owned by a removed shard always move.

Consistency guarantees during transition:

* **Single owner** - each graph node appears in exactly one shard of the new
  assignment and in at most one move, so a handoff never leaves a node owned
  by two shards once the plan is applied.
* **No loss** - every node of the input graph is present in the new
  assignment; nodes that disappeared from the graph are listed in
  ``RebalancePlan.dropped`` rather than silently discarded.
* **Chaining** - a plan's ``assignment`` is a valid ``previous`` input, so a
  worker addition/removal that arrives while a plan is still in flight is
  handled by rebalancing again from that plan (pass it as ``in_flight``).
  Nodes whose in-flight move is superseded are reported as ownership
  conflicts; the newer plan is authoritative for them.

Rebalance duration, moved nodes and ownership conflicts are exported as
Prometheus metrics when ``prometheus_client`` is installed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import networkx as nx

logger = logging.getLogger(__name__)

# Prometheus metrics (lazy import to avoid hard dependency)
_rebalance_metrics = None


def _get_rebalance_metrics():
    global _rebalance_metrics
    if _rebalance_metrics is not None:
        return _rebalance_metrics or None
    try:
        from prometheus_client import Counter, Histogram

        _rebalance_metrics = (
            Histogram(
                "ledgerlens_graph_shard_rebalance_duration_seconds",
                "Time taken to compute a graph shard rebalance plan",
            ),
            Counter(
                "ledgerlens_graph_shard_rebalance_moves_total",
                "Graph nodes moved between shards by rebalances",
            ),
            Counter(
                "ledgerlens_graph_shard_ownership_conflicts_total",
                "In-flight node moves superseded by a newer rebalance",
            ),
        )
    except ImportError:
        _rebalance_metrics = False
    return _rebalance_metrics or None


@dataclass
class ShardAssignment:
    node_to_shard: dict[str, int]
    boundary_nodes: dict[str, set[int]]
    shard_count: int
    modularity: float


@dataclass
class RebalancePlan:
    assignment: ShardAssignment
    moves: dict[str, tuple[int, int]] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)
    ownership_conflicts: set[str] = field(default_factory=set)
    duration_seconds: float = 0.0


class GraphShardPartitioner:
    def __init__(self, shard_count: int, overlap_hops: int = 1):
        if shard_count < 1:
            raise ValueError("shard_count must be >= 1")
        if overlap_hops < 0 or overlap_hops > 3:
            raise ValueError("overlap_hops must be 0-3")
        self._shard_count = shard_count
        self._overlap_hops = overlap_hops

    def partition(self, edge_data: dict[tuple[str, str], list]) -> ShardAssignment:
        undirected = nx.Graph()
        for (base, counter) in edge_data:
            undirected.add_edge(base, counter)

        nodes = list(undirected.nodes())
        if not nodes:
            return ShardAssignment(
                node_to_shard={},
                boundary_nodes={},
                shard_count=self._shard_count,
                modularity=1.0,
            )

        n = len(nodes)
        if n < self._shard_count:
            shard_count = n
        else:
            shard_count = self._shard_count

        communities = self._detect_communities(undirected, n)
        balanced = self._balance_communities(communities, n, shard_count)
        node_to_shard: dict[str, int] = {}
        for shard_idx, members in enumerate(balanced):
            for node in members:
                node_to_shard[node] = shard_idx

        modularity = self._compute_modularity(undirected, balanced, n)
        boundary_nodes = self._find_boundary_nodes(undirected, node_to_shard, shard_count)

        return ShardAssignment(
            node_to_shard=node_to_shard,
            boundary_nodes=boundary_nodes,
            shard_count=shard_count,
            modularity=modularity,
        )

    def rebalance(
        self,
        previous: ShardAssignment,
        edge_data: dict[tuple[str, str], list],
        in_flight: RebalancePlan | None = None,
    ) -> RebalancePlan:
        """Plan the migration from *previous* to this partitioner's shard count."""
        start = time.perf_counter()
        fresh = self.partition(edge_data)

        # Relabel new shards to maximise overlap with the previous assignment.
        overlap: dict[tuple[int, int], int] = {}
        for node, new_shard in fresh.node_to_shard.items():
            old_shard = previous.node_to_shard.get(node)
            if old_shard is not None and old_shard < fresh.shard_count:
                key = (new_shard, old_shard)
                overlap[key] = overlap.get(key, 0) + 1
        relabel: dict[int, int] = {}
        used: set[int] = set()
        for (new_shard, old_shard), _ in sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0])):
            if new_shard not in relabel and old_shard not in used:
                relabel[new_shard] = old_shard
                used.add(old_shard)
        free = iter(i for i in range(fresh.shard_count) if i not in used)
        for new_shard in range(fresh.shard_count):
            if new_shard not in relabel:
                relabel[new_shard] = next(free)

        node_to_shard = {n: relabel[s] for n, s in fresh.node_to_shard.items()}
        assignment = ShardAssignment(
            node_to_shard=node_to_shard,
            boundary_nodes={n: {relabel[s] for s in shards} for n, shards in fresh.boundary_nodes.items()},
            shard_count=fresh.shard_count,
            modularity=fresh.modularity,
        )

        moves: dict[str, tuple[int, int]] = {}
        for node, target in node_to_shard.items():
            source = previous.node_to_shard.get(node)
            if source is not None and source != target:
                moves[node] = (source, target)
        dropped = set(previous.node_to_shard) - set(node_to_shard)
        conflicts = set(moves) & set(in_flight.moves) if in_flight is not None else set()

        duration = time.perf_counter() - start
        metrics = _get_rebalance_metrics()
        if metrics is not None:
            duration_hist, moves_counter, conflicts_counter = metrics
            duration_hist.observe(duration)
            moves_counter.inc(len(moves))
            conflicts_counter.inc(len(conflicts))
        logger.info(
            "Graph shard rebalance %d -> %d shards: %d moves, %d dropped, %d conflicts",
            previous.shard_count, assignment.shard_count, len(moves), len(dropped), len(conflicts),
        )
        return RebalancePlan(
            assignment=assignment,
            moves=moves,
            dropped=dropped,
            ownership_conflicts=conflicts,
            duration_seconds=duration,
        )

    def _detect_communities(self, graph: nx.Graph, n: int) -> list[set[str]]:
        if n == 0:
            return []
        try:
            raw = list(nx.community.louvain_communities(graph, seed=42))
            if raw:
                return raw
        except Exception as exc:
            logger.warning("Louvain community detection failed: %s; falling back to random assignment", exc)

        nodes = list(graph.nodes())
        chunk_size = max(1, n // self._shard_count)
        return [set(nodes[i:i + chunk_size]) for i in range(0, n, chunk_size)]

    def _balance_communities(
        self, communities: list[set[str]], n: int, shard_count: int
    ) -> list[set[str]]:
        target = max(1, n // shard_count)
        merged = self._merge_small_communities(communities, target)
        result = self._split_large_communities(merged, target, shard_count)
        # Fold surplus communities into the smallest shards so shard ids stay < shard_count.
        result.sort(key=len, reverse=True)
        while len(result) > shard_count:
            surplus = result.pop()
            min(result[:shard_count], key=len).update(surplus)
        return result

    def _merge_small_communities(self, communities: list[set[str]], target: int) -> list[set[str]]:
        small: list[set[str]] = []
        large: list[set[str]] = []
        for comm in communities:
            if len(comm) < target:
                small.append(comm)
            else:
                large.append(comm)

        merged: list[set[str]] = list(large)
        current: set[str] = set()
        for comm in small:
            if not current:
                current = set(comm)
            elif len(current) + len(comm) <= target:
                current |= comm
            else:
                merged.append(current)
                current = set(comm)
        if current:
            merged.append(current)
        return merged

    def _split_large_communities(
        self, communities: list[set[str]], target: int, shard_count: int
    ) -> list[set[str]]:
        result: list[set[str]] = []
        for comm in communities:
            members = list(comm)
            while len(members) > target * 1.5 and len(result) < shard_count - 1:
                result.append(set(members[:target]))
                members = members[target:]
            result.append(set(members))
        return result

    def _find_boundary_nodes(
        self, graph: nx.Graph, node_to_shard: dict[str, int], shard_count: int
    ) -> dict[str, set[int]]:
        if self._overlap_hops == 0:
            return {}

        shard_nodes: list[set[str]] = [set() for _ in range(shard_count)]
        for node, shard in node_to_shard.items():
            shard_nodes[shard].add(node)

        boundary: dict[str, set[int]] = {}
        for u, v in graph.edges():
            su = node_to_shard.get(u)
            sv = node_to_shard.get(v)
            if su is not None and sv is not None and su != sv:
                for node, other_shard in [(u, sv), (v, su)]:
                    boundary.setdefault(node, set()).add(other_shard)

        if self._overlap_hops > 1:
            for _ in range(self._overlap_hops - 1):
                new_boundary: dict[str, set[int]] = {}
                for node, extra_shards in boundary.items():
                    for neighbor in graph.neighbors(node):
                        existing = new_boundary.setdefault(neighbor, set())
                        existing.update(extra_shards)
                        ns = node_to_shard.get(neighbor)
                        if ns is not None:
                            for es in extra_shards:
                                if es != ns:
                                    existing.add(es)
                for node, extra in new_boundary.items():
                    boundary.setdefault(node, set()).update(extra)

        return boundary

    def _compute_modularity(
        self, graph: nx.Graph, communities: list[set[str]], n: int
    ) -> float:
        if n == 0:
            return 1.0
        try:
            return nx.community.modularity(graph, communities)
        except Exception:
            return 0.0
