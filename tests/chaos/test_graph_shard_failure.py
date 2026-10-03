"""Chaos scenario #5: graph-shard node failure and rebalancing.

Kills shard-holding workers of ``ShardedTradeGraph.find_wash_rings`` and
verifies that the failed partitions are rebalanced onto surviving shards with
no data loss (every ring the unsharded engine finds is still found) within
the recovery-time SLO.

Fault-tolerance limit: up to ``shard_count - 1`` simultaneous shard failures
(at least one survivor must remain to absorb the failed partitions); losing
every shard raises ``ShardFailureError``.

Unlike the other chaos scenarios this one needs no Toxiproxy, so it overrides
the session-level ``require_toxiproxy`` skip.

Run with::

    pytest tests/chaos/test_graph_shard_failure.py -m chaos -v
"""

from __future__ import annotations

import os
import time

import pytest

from detection.graph_engine import (
    ShardedTradeGraph,
    ShardFailureError,
    TradeGraph,
    _run_shard_find_rings,
)

pytestmark = pytest.mark.chaos

SHARD_COUNT = 4
RING_COUNT = 8
RING_SIZE = 4
RECOVERY_SLO_SECONDS = 30.0


@pytest.fixture(scope="session", autouse=True)
def require_toxiproxy() -> None:
    """Shard-failure scenarios run in-process and do not need Toxiproxy."""


class _Trade:
    def __init__(self, base: str, counter: str) -> None:
        self.base_account = base
        self.counter_account = counter
        self.base_amount = 100.0
        self.ledger_close_time = "2026-06-12T00:00:00Z"


def _load(graph: TradeGraph) -> TradeGraph:
    for r in range(RING_COUNT):
        accounts = [f"R{r}_A{i}" for i in range(RING_SIZE)]
        for i, acct in enumerate(accounts):
            graph.add_trade(_Trade(acct, accounts[(i + 1) % RING_SIZE]))
    return graph


def _ring_sets(rings: list[dict]) -> set[frozenset[str]]:
    return {frozenset(r["accounts"]) for r in rings}


def _kill_shard_zero(worker_input: tuple) -> tuple[int, list[dict]]:
    """Hard-kill the process holding shard 0 mid-operation."""
    if worker_input[0] == 0:
        time.sleep(3)  # let the surviving shards finish first
        os._exit(1)
    return _run_shard_find_rings(worker_input)


class _KillShardZeroGraph(ShardedTradeGraph):
    _shard_worker = staticmethod(_kill_shard_zero)


def _graph_failing(shard_ids: set[int]) -> ShardedTradeGraph:
    def worker(worker_input: tuple) -> tuple[int, list[dict]]:
        if worker_input[0] in shard_ids:
            raise ConnectionError(f"shard {worker_input[0]} node lost")
        return _run_shard_find_rings(worker_input)

    cls = type("_FailingGraph", (ShardedTradeGraph,), {"_shard_worker": staticmethod(worker)})
    return cls(shard_count=SHARD_COUNT, overlap_hops=1, max_workers=1)


@pytest.fixture(scope="module")
def expected_rings() -> set[frozenset[str]]:
    rings = _ring_sets(_load(TradeGraph()).find_wash_rings())
    assert len(rings) == RING_COUNT
    return rings


def _assert_recovered(graph: ShardedTradeGraph, rings, expected, failed, elapsed):
    assert sorted(graph.failed_shards) == sorted(failed)
    assert elapsed < RECOVERY_SLO_SECONDS
    # Data integrity: every ring survives the failure.
    assert _ring_sets(rings) == expected
    # Rebalancing: every account now lives on a surviving shard.
    node_to_shard = graph._shard_assignment.node_to_shard
    assert len(node_to_shard) == RING_COUNT * RING_SIZE
    assert not set(node_to_shard.values()) & set(failed)
    for ring in rings:
        assert not set(ring["shard_ids"]) & set(failed)


def test_worker_killed_mid_operation_rebalances_without_data_loss(expected_rings):
    graph = _load(
        _KillShardZeroGraph(shard_count=SHARD_COUNT, overlap_hops=1, max_workers=SHARD_COUNT)
    )
    start = time.monotonic()
    rings = graph.find_wash_rings()
    elapsed = time.monotonic() - start

    assert 0 in graph.failed_shards
    _assert_recovered(graph, rings, expected_rings, graph.failed_shards, elapsed)


@pytest.mark.parametrize("failures", range(1, SHARD_COUNT))
def test_simultaneous_multi_node_failure_up_to_tolerance_limit(expected_rings, failures):
    failed = set(range(failures))
    graph = _load(_graph_failing(failed))
    start = time.monotonic()
    rings = graph.find_wash_rings()
    elapsed = time.monotonic() - start

    _assert_recovered(graph, rings, expected_rings, failed, elapsed)


def test_failure_beyond_tolerance_limit_raises():
    graph = _load(_graph_failing(set(range(SHARD_COUNT))))
    with pytest.raises(ShardFailureError):
        graph.find_wash_rings()
