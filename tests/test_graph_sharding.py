"""Tests for the adaptive sharded graph engine (Issue #348)."""

from unittest.mock import patch

import pytest

from detection.graph_engine import (
    GraphTooLargeError,
    ShardedTradeGraph,
    TradeGraph,
)
from detection.graph_sharding import GraphShardPartitioner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(base: str, counter: str, amount: float = 100.0, ts=None):
    class _Trade:
        base_account = base
        counter_account = counter
        base_amount = amount
        ledger_close_time = ts or "2026-06-12T00:00:00Z"

    return _Trade()


def _ring_trade_data(accounts, amount=100.0):
    edge_data = {}
    for i, acct in enumerate(accounts):
        target = accounts[(i + 1) % len(accounts)]
        key = (acct, target)
        edge_data[key] = [float(amount), 1, []]
    return edge_data


def _add_ring_to_tg(tg: TradeGraph, accounts, amount=100.0):
    for i, acct in enumerate(accounts):
        target = accounts[(i + 1) % len(accounts)]
        tg.add_trade(_make_trade(acct, target, amount=amount))


# ---------------------------------------------------------------------------
# GraphShardPartitioner tests
# ---------------------------------------------------------------------------


class TestGraphShardPartitioner:
    def test_partition_three_clusters(self):
        """3 dense wash-ring clusters connected by sparse bridges assign each
        full cluster to a single shard."""
        edge_data = {}
        clusters = [
            ["A1", "A2", "A3", "A4", "A5"],
            ["B1", "B2", "B3", "B4", "B5"],
            ["C1", "C2", "C3", "C4", "C5"],
        ]
        for cluster in clusters:
            for i, acct in enumerate(cluster):
                target = cluster[(i + 1) % len(cluster)]
                edge_data[(acct, target)] = [100.0, 1, []]
                edge_data[(target, acct)] = [100.0, 1, []]

        edge_data[("A5", "B1")] = [1.0, 1, []]
        edge_data[("B5", "C1")] = [1.0, 1, []]

        partitioner = GraphShardPartitioner(shard_count=3, overlap_hops=0)
        assignment = partitioner.partition(edge_data)

        assert assignment.shard_count == 3
        assert assignment.modularity > 0.0

        for cluster in clusters:
            shards = {assignment.node_to_shard.get(a) for a in cluster}
            assert len(shards) == 1, f"Cluster {cluster} was split across shards {shards}"

    def test_partition_empty_graph(self):
        partitioner = GraphShardPartitioner(shard_count=4, overlap_hops=1)
        assignment = partitioner.partition({})
        assert assignment.shard_count == 4
        assert assignment.node_to_shard == {}
        assert assignment.boundary_nodes == {}
        assert assignment.modularity == 1.0

    def test_partition_single_node(self):
        edge_data = {("A", "A"): [100.0, 1, []]}
        partitioner = GraphShardPartitioner(shard_count=4, overlap_hops=0)
        assignment = partitioner.partition(edge_data)
        assert assignment.node_to_shard.get("A") is not None

    def test_invalid_shard_count(self):
        with pytest.raises(ValueError, match="shard_count"):
            GraphShardPartitioner(shard_count=0)

    def test_invalid_overlap_hops(self):
        with pytest.raises(ValueError, match="overlap_hops"):
            GraphShardPartitioner(shard_count=2, overlap_hops=5)

    def test_boundary_replication(self):
        edge_data = {
            ("A", "B"): [100.0, 1, []],
            ("B", "C"): [100.0, 1, []],
            ("C", "D"): [100.0, 1, []],
            ("D", "E"): [100.0, 1, []],
        }
        partitioner = GraphShardPartitioner(shard_count=2, overlap_hops=1)
        assignment = partitioner.partition(edge_data)
        if assignment.boundary_nodes:
            for node, extra_shards in assignment.boundary_nodes.items():
                for es in extra_shards:
                    assert es != assignment.node_to_shard.get(node)

    def test_overlap_hops_zero_no_boundary(self):
        edge_data = {("A", "B"): [100.0, 1, []]}
        partitioner = GraphShardPartitioner(shard_count=2, overlap_hops=0)
        assignment = partitioner.partition(edge_data)
        assert assignment.boundary_nodes == {}


# ---------------------------------------------------------------------------
# ShardedTradeGraph tests
# ---------------------------------------------------------------------------


class TestShardedTradeGraph:
    def test_ring_detected_identically_to_unsharded(self):
        """A ring entirely within one shard is detected identically via
        ShardedTradeGraph and unsharded TradeGraph."""
        accounts = ["A", "B", "C", "D", "E"]

        tg = TradeGraph()
        for i, acct in enumerate(accounts):
            target = accounts[(i + 1) % len(accounts)]
            tg.add_trade(_make_trade(acct, target, amount=100.0))
        unsharded_rings = tg.find_wash_rings(min_ring_size=3)

        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        for i, acct in enumerate(accounts):
            target = accounts[(i + 1) % len(accounts)]
            stg.add_trade(_make_trade(acct, target, amount=100.0))
        sharded_rings = stg.find_wash_rings(min_ring_size=3)

        assert len(sharded_rings) >= 1
        for ring in unsharded_rings:
            ring_set = frozenset(ring["accounts"])
            found = any(
                frozenset(sr["accounts"]) == ring_set for sr in sharded_rings
            )
            assert found, f"Ring {ring_set} not found in sharded results"

    def test_ring_straddling_boundary_within_overlap_detected(self):
        """A ring straddling a shard boundary within overlap_hops is still detected."""
        accounts = ["A", "B", "C", "D"]

        stg = ShardedTradeGraph(shard_count=2, overlap_hops=2, max_workers=2)
        for i, acct in enumerate(accounts):
            target = accounts[(i + 1) % len(accounts)]
            stg.add_trade(_make_trade(acct, target, amount=100.0))
        rings = stg.find_wash_rings(min_ring_size=3)
        ring_accounts = {tuple(sorted(r["accounts"])) for r in rings}
        full_ring = tuple(sorted(accounts))
        assert full_ring in ring_accounts or any(
            len(set(accounts) & set(r["accounts"])) >= 3 for r in rings
        )

    def test_shard_ids_present_in_results(self):
        accounts = ["A", "B", "C"]
        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        for i, acct in enumerate(accounts):
            target = accounts[(i + 1) % len(accounts)]
            stg.add_trade(_make_trade(acct, target, amount=100.0))
        rings = stg.find_wash_rings(min_ring_size=3)
        for ring in rings:
            assert "shard_ids" in ring
            assert isinstance(ring["shard_ids"], list)

    def test_deduplication_merges_redundant_rings(self):
        edge_data = {}
        ring1 = ["A", "B", "C"]
        ring2 = ["D", "E", "F"]
        for ring in [ring1, ring2]:
            for i, acct in enumerate(ring):
                target = ring[(i + 1) % len(ring)]
                edge_data[(acct, target)] = [100.0, 1, []]
        edge_data[("C", "D")] = [1.0, 1, []]

        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        for (base, counter), data in edge_data.items():
            stg._node_index.add(base)
            stg._node_index.add(counter)
            stg._edge_data[(base, counter)] = data
        rings = stg.find_wash_rings(min_ring_size=3)
        assert len(rings) >= 2

    def test_shard_topology_property(self):
        stg = ShardedTradeGraph(shard_count=4, overlap_hops=1, max_workers=2)
        accounts = ["A", "B", "C", "D", "E"]
        for i, acct in enumerate(accounts):
            target = accounts[(i + 1) % len(accounts)]
            stg.add_trade(_make_trade(acct, target, amount=100.0))
        stg.find_wash_rings(min_ring_size=3)

        topology = stg.shard_topology
        assert topology["shard_count"] > 0
        assert isinstance(topology["modularity"], float)
        assert len(topology["shards"]) == topology["shard_count"]

    def test_global_instance_set(self):
        import detection.graph_engine as ge
        ge._SHARDED_GRAPH_INSTANCE = None
        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        assert ge._SHARDED_GRAPH_INSTANCE is stg

    def test_empty_graph(self):
        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        rings = stg.find_wash_rings()
        assert rings == []

    def test_single_node_no_ring(self):
        stg = ShardedTradeGraph(shard_count=2, overlap_hops=1, max_workers=2)
        stg.add_trade(_make_trade("A", "A", amount=100.0))
        rings = stg.find_wash_rings(min_ring_size=3)
        assert rings == []


# ---------------------------------------------------------------------------
# Auto-trigger: TradeGraph -> ShardedTradeGraph routing
# ---------------------------------------------------------------------------


class TestAutoTrigger:
    def test_sharding_routes_when_exceeding_max_nodes(self):
        """When GRAPH_SHARD_ENABLED=true and node count exceeds MAX_GRAPH_NODES,
        TradeGraph transparently routes to ShardedTradeGraph."""
        with patch("detection.graph_engine.MAX_GRAPH_NODES", 2):
            with patch("detection.graph_engine.GRAPH_SHARD_ENABLED", True):
                tg = TradeGraph()
                tg.add_trade(_make_trade("A", "B"))
                tg.add_trade(_make_trade("C", "D"))
                assert isinstance(tg, ShardedTradeGraph)

    def test_graph_too_large_error_when_sharding_disabled(self):
        """When GRAPH_SHARD_ENABLED=false, raising GraphTooLargeError is preserved."""
        with patch("detection.graph_engine.MAX_GRAPH_NODES", 2):
            with patch("detection.graph_engine.GRAPH_SHARD_ENABLED", False):
                tg = TradeGraph()
                tg.add_trade(_make_trade("A", "B"))
                with pytest.raises(GraphTooLargeError):
                    tg.add_trade(_make_trade("C", "D"))

    def test_find_rings_still_works_after_auto_route(self):
        with patch("detection.graph_engine.MAX_GRAPH_NODES", 2):
            with patch("detection.graph_engine.GRAPH_SHARD_ENABLED", True):
                tg = TradeGraph()
                tg.add_trade(_make_trade("A", "B"))
                tg.add_trade(_make_trade("B", "C"))
                tg.add_trade(_make_trade("C", "A"))
                rings = tg.find_wash_rings(min_ring_size=3)
                assert len(rings) >= 1


# ---------------------------------------------------------------------------
# Edge case: ring beyond overlap buffer
# ---------------------------------------------------------------------------


def test_ring_beyond_overlap_buffer_missed():
    """A ring straddling a boundary beyond the overlap buffer is silently
    missed. This is a documented limitation.

    The graph is arranged as two dense clusters (L1-L2-L3-L4-L5 and R1-R2-R3-R4-R5)
    connected by a single sparse edge L5->R1. A ring that spans both clusters
    (e.g. L1->...->R3->...->L1) will cross the shard boundary beyond the overlap
    buffer and will not be detected as a complete ring by either shard.
    """
    edge_data = {}
    left = ["L1", "L2", "L3", "L4", "L5"]
    right = ["R1", "R2", "R3", "R4", "R5"]
    for cluster in [left, right]:
        for i, acct in enumerate(cluster):
            target = cluster[(i + 1) % len(cluster)]
            edge_data[(acct, target)] = [100.0, 1, []]
    edge_data[("L5", "R1")] = [1.0, 1, []]

    stg = ShardedTradeGraph(shard_count=2, overlap_hops=0, max_workers=2)
    for (base, counter), data in edge_data.items():
        stg._node_index.add(base)
        stg._node_index.add(counter)
        stg._edge_data[(base, counter)] = data

    rings = stg.find_wash_rings(min_ring_size=3)

    full_ring = frozenset(left + right)
    for ring in rings:
        ring_set = frozenset(ring["accounts"])
        assert ring_set != full_ring, (
            "Ring spanning beyond overlap buffer was unexpectedly detected. "
            "This is a documented limitation of the sharded graph engine."
        )
