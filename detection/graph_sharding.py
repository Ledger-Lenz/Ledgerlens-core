"""Adaptive sharded graph engine partitioner using community detection."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


@dataclass
class ShardAssignment:
    node_to_shard: dict[str, int]
    boundary_nodes: dict[str, set[int]]
    shard_count: int
    modularity: float


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
        return self._split_large_communities(merged, target, shard_count)

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
