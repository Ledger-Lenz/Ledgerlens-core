"""Tests for IterativeTarjanSCC, NodeIndex, SparseTradeGraph, and TradeGraph."""

from __future__ import annotations

import time
import tracemalloc
from datetime import datetime, timezone
from types import SimpleNamespace

import networkx as nx
import pytest

from detection.graph_engine import (
    GraphTooLargeError,
    IterativeTarjanSCC,
    NodeIndex,
    SparseTradeGraph,
    TradeGraph,
)
from ingestion.data_models import Asset, Trade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NATIVE = Asset(code="XLM", issuer=None)
_USDC = Asset(
    code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
)
_BASE_TS = datetime(2026, 6, 12, tzinfo=timezone.utc)


def _make_trade(base: str, counter: str, amount: float = 100.0, ts: datetime | None = None) -> Trade:
    return Trade(
        id="t1",
        ledger_close_time=ts or _BASE_TS,
        base_account=base,
        counter_account=counter,
        base_asset=_NATIVE,
        counter_asset=_USDC,
        base_amount=amount,
        counter_amount=amount * 0.1,
        price=0.1,
        base_is_seller=True,
    )


def _mock_trade(base: str, counter: str, amount: float = 1.0) -> SimpleNamespace:
    """Lightweight mock – avoids Pydantic validation overhead in perf tests."""
    return SimpleNamespace(
        base_account=base,
        counter_account=counter,
        base_amount=amount,
        ledger_close_time=None,
    )


def _nx_sccs(graph: dict[int, list[int]]) -> list[frozenset[int]]:
    """Reference SCCs via networkx for comparison.

    Also adds nodes that have no outgoing edges (sinks) so the comparison
    includes all nodes that appear as keys in the adjacency dict.
    """
    g = nx.DiGraph()
    for src, dsts in graph.items():
        g.add_node(src)  # ensure sink nodes (empty adjacency) are included
        for dst in dsts:
            g.add_edge(src, dst)
    return [frozenset(c) for c in nx.strongly_connected_components(g)]


def _tarjan_sccs(graph: dict[int, list[int]]) -> list[frozenset[int]]:
    return [frozenset(scc) for scc in IterativeTarjanSCC().run(graph)]


# ---------------------------------------------------------------------------
# Correctness — SCC output equivalence with networkx
# ---------------------------------------------------------------------------


def test_scc_equivalence_three_node_ring():
    g = {0: [1], 1: [2], 2: [0]}
    assert set(map(frozenset, IterativeTarjanSCC().run(g))) == set(_nx_sccs(g))


def test_scc_equivalence_disconnected_two_rings():
    g = {0: [1], 1: [0], 2: [3], 3: [2], 4: []}
    assert set(_tarjan_sccs(g)) == set(_nx_sccs(g))


def test_scc_equivalence_dag():
    g = {0: [1, 2], 1: [3], 2: [3], 3: []}
    assert set(_tarjan_sccs(g)) == set(_nx_sccs(g))


def test_scc_equivalence_single_large_ring():
    n = 50
    g = {i: [(i + 1) % n] for i in range(n)}
    assert set(_tarjan_sccs(g)) == set(_nx_sccs(g))


def test_scc_all_nodes_appear_exactly_once():
    g = {0: [1], 1: [2], 2: [0], 3: [4], 4: []}
    sccs = IterativeTarjanSCC().run(g)
    all_nodes = [node for scc in sccs for node in scc]
    assert sorted(all_nodes) == sorted(g.keys())


# ---------------------------------------------------------------------------
# Correctness — no recursion limit on a 2 000-node linear chain
# ---------------------------------------------------------------------------


def test_no_recursion_limit_linear_chain_2000():
    n = 2000
    g = {i: [i + 1] for i in range(n - 1)}
    g[n - 1] = []
    # Would raise RecursionError with a naive recursive implementation.
    sccs = IterativeTarjanSCC().run(g)
    assert len(sccs) == n
    for scc in sccs:
        assert len(scc) == 1


# ---------------------------------------------------------------------------
# Correctness — self-loop handling
# ---------------------------------------------------------------------------


def test_self_loop_no_infinite_loop_singleton():
    g = {0: [0]}
    sccs = IterativeTarjanSCC().run(g)
    assert len(sccs) == 1
    assert sccs[0] == [0]


def test_self_loop_mixed_with_real_ring():
    # 0->0 (self-loop), 1->2->1 (real ring), 3 (isolated)
    g = {0: [0], 1: [2], 2: [1], 3: []}
    sccs = IterativeTarjanSCC().run(g)
    scc_sets = {frozenset(s) for s in sccs}
    assert frozenset({0}) in scc_sets
    assert frozenset({1, 2}) in scc_sets
    assert frozenset({3}) in scc_sets


# ---------------------------------------------------------------------------
# Correctness — disconnected graph
# ---------------------------------------------------------------------------


def test_disconnected_graph_all_nodes_in_exactly_one_scc():
    g = {0: [1], 1: [], 2: [3], 3: [2]}
    sccs = IterativeTarjanSCC().run(g)
    all_nodes = [node for scc in sccs for node in scc]
    assert sorted(all_nodes) == [0, 1, 2, 3]
    assert len(sccs) == 3  # {0}, {1}, {2,3}


# ---------------------------------------------------------------------------
# Unit — NodeIndex bijection
# ---------------------------------------------------------------------------


def test_node_index_bijection_1000_nodes():
    idx = NodeIndex()
    nodes = [f"G{'A' * 5}{i:050d}" for i in range(1000)]
    for node in nodes:
        idx.add(node)
    for node in nodes:
        assert idx.get_node(idx.get_id(node)) == node  # type: ignore[arg-type]


def test_node_index_add_idempotent():
    idx = NodeIndex()
    first = idx.add("GABC")
    second = idx.add("GABC")
    assert first == second
    assert len(idx) == 1


def test_node_index_get_id_unknown_returns_none():
    idx = NodeIndex()
    idx.add("GABC")
    assert idx.get_id("GUNKNOWN") is None


# ---------------------------------------------------------------------------
# Unit — SparseTradeGraph.to_adjacency_dict
# ---------------------------------------------------------------------------


def test_sparse_trade_graph_to_adjacency_dict_five_trades():
    idx = NodeIndex()
    for acct in ["A", "B", "C", "D", "E"]:
        idx.add(acct)

    trades = [
        _make_trade("A", "B"),
        _make_trade("B", "C"),
        _make_trade("C", "A"),
        _make_trade("D", "E"),
        _make_trade("E", "D"),
    ]
    stg = SparseTradeGraph(idx)
    stg.build_from_trades(trades)
    adj = stg.to_adjacency_dict()

    a, b, c, d, e = idx.get_id("A"), idx.get_id("B"), idx.get_id("C"), idx.get_id("D"), idx.get_id("E")
    assert b in adj.get(a, [])
    assert c in adj.get(b, [])
    assert a in adj.get(c, [])
    assert e in adj.get(d, [])
    assert d in adj.get(e, [])


def test_sparse_trade_graph_build_from_trades_skips_null_counter():
    idx = NodeIndex()
    idx.add("A")
    idx.add("B")
    trades = [
        _make_trade("A", "B"),
        SimpleNamespace(base_account="A", counter_account=None, base_amount=10.0),
    ]
    stg = SparseTradeGraph(idx)
    stg.build_from_trades(trades)  # must not raise
    adj = stg.to_adjacency_dict()
    assert idx.get_id("B") in adj.get(idx.get_id("A"), [])


# ---------------------------------------------------------------------------
# Unit — GraphTooLargeError
# ---------------------------------------------------------------------------


def test_graph_too_large_error_trade_graph(monkeypatch):
    import detection.graph_engine as ge
    monkeypatch.setattr(ge, "MAX_GRAPH_NODES", 2)

    tg = TradeGraph()
    tg.add_trade(_make_trade("A", "B"))  # 2 unique nodes — exactly at limit, OK
    with pytest.raises(GraphTooLargeError):
        tg.add_trade(_make_trade("A", "C"))  # third unique node pushes n=3 > 2


def test_graph_too_large_error_sparse_trade_graph(monkeypatch):
    import detection.graph_engine as ge
    monkeypatch.setattr(ge, "MAX_GRAPH_NODES", 1)

    idx = NodeIndex()
    trades = [_make_trade("A", "B")]  # two nodes
    stg = SparseTradeGraph(idx)
    with pytest.raises(GraphTooLargeError):
        stg.build_from_trades(trades)


# ---------------------------------------------------------------------------
# TradeGraph public API correctness
# ---------------------------------------------------------------------------


def test_trade_graph_three_ring_find_wash_rings():
    tg = TradeGraph()
    for base, counter in [("A", "B"), ("B", "C"), ("C", "A")]:
        tg.add_trade(_make_trade(base, counter, 100.0))

    rings = tg.find_wash_rings()
    assert len(rings) == 1
    assert rings[0]["accounts"] == ["A", "B", "C"]
    assert rings[0]["total_volume"] == 300.0
    assert rings[0]["cycle_volume"] == 100.0
    assert rings[0]["truncated"] is False


def test_trade_graph_no_ring_returns_empty():
    tg = TradeGraph()
    tg.add_trade(_make_trade("A", "B"))
    tg.add_trade(_make_trade("B", "C"))
    rings = tg.find_wash_rings()
    assert rings == []


def test_trade_graph_get_ring_members_in_ring():
    tg = TradeGraph()
    for base, counter in [("A", "B"), ("B", "C"), ("C", "A")]:
        tg.add_trade(_make_trade(base, counter, 100.0))

    meta = tg.get_ring_members("A")
    assert meta is not None
    assert meta["wash_ring_size"] == 3.0
    assert meta["cycle_volume"] == 100.0


def test_trade_graph_get_ring_members_not_in_ring():
    tg = TradeGraph()
    tg.add_trade(_make_trade("A", "B"))
    tg.add_trade(_make_trade("B", "C"))
    assert tg.get_ring_members("A") is None


def test_trade_graph_invalid_min_ring_size():
    tg = TradeGraph()
    with pytest.raises(ValueError, match="min_ring_size must be at least 1"):
        tg.find_wash_rings(min_ring_size=0)


def test_trade_graph_max_less_than_min():
    tg = TradeGraph()
    with pytest.raises(ValueError, match="max_ring_size must be greater than or equal to min_ring_size"):
        tg.find_wash_rings(min_ring_size=5, max_ring_size=3)


def test_trade_graph_truncated_ring():
    tg = TradeGraph()
    accounts = [f"W{i}" for i in range(15)]
    for i in range(len(accounts)):
        tg.add_trade(_make_trade(accounts[i], accounts[(i + 1) % len(accounts)], 100.0))

    rings = tg.find_wash_rings(max_ring_size=10)
    assert len(rings) == 1
    assert rings[0]["truncated"] is True
    assert rings[0]["total_volume"] == 1500.0


def test_trade_graph_self_loop_does_not_form_multi_node_ring():
    tg = TradeGraph()
    tg.add_trade(_make_trade("A", "A", 50.0))
    rings = tg.find_wash_rings()
    assert rings == []


def test_trade_graph_result_identical_to_module_function():
    """TradeGraph must produce the same rings as the module-level find_wash_rings."""
    import pandas as pd
    from detection.graph_engine import build_transaction_graph, find_wash_rings

    rows = [
        {"base_account": "A", "counter_account": "B", "base_amount": 100.0, "ledger_close_time": _BASE_TS},
        {"base_account": "B", "counter_account": "C", "base_amount": 100.0, "ledger_close_time": _BASE_TS},
        {"base_account": "C", "counter_account": "A", "base_amount": 100.0, "ledger_close_time": _BASE_TS},
    ]
    df = pd.DataFrame(rows)
    nx_graph = build_transaction_graph(df)
    reference_rings = find_wash_rings(nx_graph)

    tg = TradeGraph()
    for row in rows:
        tg.add_trade(_make_trade(row["base_account"], row["counter_account"], row["base_amount"], row["ledger_close_time"]))
    tg_rings = tg.find_wash_rings()

    assert len(tg_rings) == len(reference_rings)
    for tg_ring, ref_ring in zip(tg_rings, reference_rings):
        assert tg_ring["accounts"] == ref_ring["accounts"]
        assert tg_ring["total_volume"] == pytest.approx(ref_ring["total_volume"])
        assert tg_ring["cycle_volume"] == pytest.approx(ref_ring["cycle_volume"])
        assert tg_ring["truncated"] == ref_ring["truncated"]


def test_trade_graph_cache_invalidated_after_add_trade():
    tg = TradeGraph()
    tg.add_trade(_make_trade("A", "B"))
    rings1 = tg.find_wash_rings()
    assert rings1 == []

    # Add the closing edge to form a ring.
    tg.add_trade(_make_trade("B", "C"))
    tg.add_trade(_make_trade("C", "A"))
    rings2 = tg.find_wash_rings()
    assert len(rings2) == 1


# ---------------------------------------------------------------------------
# Performance — 100 K nodes, 500 K edges in < 30 s and < 500 MB peak RAM
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_performance_100k_nodes_500k_edges():
    import numpy as np

    n_nodes = 100_000
    n_edges = 500_000
    rng = np.random.default_rng(42)

    src_ids = rng.integers(0, n_nodes, n_edges).tolist()
    dst_ids = rng.integers(0, n_nodes, n_edges).tolist()

    # --- ingestion phase (untimed for tracemalloc; tracemalloc skews add_trade timing) ---
    tg = TradeGraph()
    ingest_start = time.perf_counter()
    for src, dst in zip(src_ids, dst_ids):
        tg.add_trade(_mock_trade(f"G{src:06d}", f"G{dst:06d}"))
    ingest_time = time.perf_counter() - ingest_start

    # --- graph analysis phase (timed + memory profiled) ---
    tracemalloc.start()
    ring_start = time.perf_counter()
    rings = tg.find_wash_rings()
    ring_time = time.perf_counter() - ring_start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    total_time = ingest_time + ring_time

    assert total_time < 30.0, f"Total time {total_time:.1f}s exceeds 30s target"
    assert peak < 500 * 1024 * 1024, f"Peak RAM {peak / 1e6:.1f} MB exceeds 500 MB target"
    assert isinstance(rings, list)
    print(
        f"\n[perf] ingest: {ingest_time:.2f}s | ring-find: {ring_time:.2f}s | "
        f"total: {total_time:.2f}s | peak RAM: {peak / 1e6:.1f} MB | rings: {len(rings)}"
    )
