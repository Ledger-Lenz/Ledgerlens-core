import statistics
import time

import networkx as nx
import pandas as pd
import pytest

from detection.graph_engine import (
    add_path_payment_edges,
    build_ring_membership_index,
    build_transaction_graph,
    find_wash_rings,
)


def _ring_trades(accounts, times, volume=100.0) -> pd.DataFrame:
    rows = []
    for i, account in enumerate(accounts):
        rows.append(
            {
                "ledger_close_time": times[i],
                "base_account": account,
                "counter_account": accounts[(i + 1) % len(accounts)],
                "base_amount": volume,
            }
        )
    return pd.DataFrame(rows)


def _seconds(value: int) -> pd.Timedelta:
    return pd.Timedelta(seconds=value)


def test_three_account_ring_detected_with_cycle_volume():
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    trades = _ring_trades(["A", "B", "C"], [base, base + _seconds(60), base + _seconds(120)])

    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)

    assert len(rings) == 1
    ring = rings[0]
    assert ring["accounts"] == ["A", "B", "C"]
    assert ring["total_volume"] == 300.0
    assert ring["cycle_volume"] == 100.0
    assert ring["avg_trade_count"] == 1.0
    assert ring["timing_tightness"] == 0.0
    assert ring["truncated"] is False


def test_five_account_ring_timing_tightness():
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    times = [
        base,
        base + _seconds(10),
        base + _seconds(20),
        base + _seconds(40),
        base + _seconds(100),
    ]
    trades = _ring_trades(["A", "B", "C", "D", "E"], times)

    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)

    intervals = [10.0, 10.0, 20.0, 60.0]
    assert rings[0]["timing_tightness"] == statistics.pstdev(intervals)


def test_large_scc_is_truncated_not_enumerated():
    accounts = [f"W{i}" for i in range(15)]
    rows = []
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    for i, account in enumerate(accounts):
        rows.append(
            {
                "ledger_close_time": base + _seconds(i),
                "base_account": account,
                "counter_account": accounts[(i + 1) % len(accounts)],
                "base_amount": 100.0,
            }
        )
    trades = pd.DataFrame(rows)
    graph = build_transaction_graph(trades)

    rings = find_wash_rings(graph, max_ring_size=10)

    assert len(rings) == 1
    assert rings[0]["accounts"] == sorted(accounts)
    assert rings[0]["total_volume"] == 1500.0
    assert rings[0]["cycle_volume"] == 750.0
    assert rings[0]["truncated"] is True


def test_ring_membership_and_cycle_volume_ratio():
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    trades = _ring_trades(["A", "B", "C"], [base, base + _seconds(1), base + _seconds(2)])
    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)
    membership = build_ring_membership_index(rings, trades=trades)

    assert set(membership) == {"A", "B", "C"}
    for account, metadata in membership.items():
        assert metadata["wash_ring_size"] == 3.0
        assert metadata["cycle_volume_ratio"] == 1.0
        assert metadata["timing_tightness_score"] == 1.0

    assert "D" not in membership


def test_build_ring_membership_index_with_graph():
    graph = nx.DiGraph()
    graph.add_edge("A", "B", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("B", "C", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("C", "A", total_volume=10.0, trade_count=1, timestamps=[])
    rings = find_wash_rings(graph)
    membership = build_ring_membership_index(rings, graph=graph)
    assert set(membership) == {"A", "B", "C"}


def test_build_ring_membership_index_empty():
    assert build_ring_membership_index([]) == {}


def test_build_ring_membership_index_prefers_larger_ring():
    graph = nx.DiGraph()
    graph.add_edge("A", "B", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("B", "C", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("C", "A", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("A", "D", total_volume=5.0, trade_count=1, timestamps=[])
    graph.add_edge("D", "A", total_volume=5.0, trade_count=1, timestamps=[])
    rings = find_wash_rings(graph)
    membership = build_ring_membership_index(rings, graph=graph)
    for account in ["A", "B", "C"]:
        assert membership[account]["wash_ring_size"] == 3.0


def test_build_transaction_graph_aggregates_edges_and_self_loops():
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    trades = pd.DataFrame(
        [
            {"ledger_close_time": base, "base_account": "A", "counter_account": "B", "base_amount": 10.0},
                {"ledger_close_time": base + _seconds(1), "base_account": "A", "counter_account": "B", "base_amount": 20.0},
                {"ledger_close_time": base + _seconds(2), "base_account": "C", "counter_account": "C", "base_amount": 5.0},

        ]
    )

    graph = build_transaction_graph(trades)

    assert graph["A"]["B"]["total_volume"] == 30.0
    assert graph["A"]["B"]["trade_count"] == 2
    assert graph["C"]["C"]["total_volume"] == 5.0
    assert graph["C"]["C"]["trade_count"] == 1


def test_build_transaction_graph_missing_columns():
    trades = pd.DataFrame({"base_account": ["A"]})
    with pytest.raises(ValueError, match="missing required columns"):
        build_transaction_graph(trades)


def test_build_transaction_graph_empty():
    graph = build_transaction_graph(pd.DataFrame())
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


def test_add_path_payment_edges_empty():
    graph = nx.DiGraph()
    add_path_payment_edges(graph, pd.DataFrame())
    assert graph.number_of_edges() == 0


def test_add_path_payment_edges_missing_columns():
    graph = nx.DiGraph()
    payments = pd.DataFrame({"source_account": ["A"]})
    with pytest.raises(ValueError, match="missing required columns"):
        add_path_payment_edges(graph, payments)


def test_add_path_payment_edges_creates_edges():
    graph = nx.DiGraph()
    payments = pd.DataFrame({
        "source_account": ["A", "B", "C"],
        "destination_account": ["B", "C", "A"],
        "source_amount": [10.0, 20.0, 30.0],
        "source_asset": ["XLM", "XLM", "XLM"],
        "destination_asset": ["USDC", "USDC", "USDC"],
        "timestamp": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")],
    })
    add_path_payment_edges(graph, payments)
    assert graph.has_edge("A", "B")
    assert graph.has_edge("B", "C")
    assert graph.has_edge("C", "A")
    assert graph["A"]["B"]["total_volume"] == 10.0
    assert graph["A"]["B"]["payment_count"] == 1
    assert len(graph["A"]["B"]["path_payments"]) == 1


def test_add_path_payment_edges_aggregates_multiple_payments():
    graph = nx.DiGraph()
    payments = pd.DataFrame({
        "source_account": ["A", "A"],
        "destination_account": ["B", "B"],
        "source_amount": [10.0, 20.0],
        "source_asset": ["XLM", "XLM"],
        "destination_asset": ["USDC", "USDC"],
        "timestamp": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
    })
    add_path_payment_edges(graph, payments)
    assert graph["A"]["B"]["total_volume"] == 30.0
    assert graph["A"]["B"]["payment_count"] == 2
    assert len(graph["A"]["B"]["path_payments"]) == 2


def test_add_path_payment_edges_skips_empty_accounts():
    graph = nx.DiGraph()
    payments = pd.DataFrame({
        "source_account": ["", "A"],
        "destination_account": ["B", ""],
        "source_amount": [10.0, 20.0],
        "source_asset": ["XLM", "XLM"],
        "destination_asset": ["USDC", "USDC"],
        "timestamp": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
    })
    add_path_payment_edges(graph, payments)
    assert graph.number_of_edges() == 0


def test_add_path_payment_edges_with_transaction_hash():
    graph = nx.DiGraph()
    payments = pd.DataFrame({
        "source_account": ["A"],
        "destination_account": ["B"],
        "source_amount": [10.0],
        "source_asset": ["XLM"],
        "destination_asset": ["USDC"],
        "timestamp": [pd.Timestamp("2026-01-01")],
        "transaction_hash": ["abc123"],
    })
    add_path_payment_edges(graph, payments)
    assert graph["A"]["B"]["path_payments"][0]["transaction_hash"] == "abc123"


def test_find_wash_rings_invalid_min_ring_size():
    graph = nx.DiGraph()
    with pytest.raises(ValueError, match="min_ring_size must be at least 1"):
        find_wash_rings(graph, min_ring_size=0)


def test_find_wash_rings_max_smaller_than_min():
    graph = nx.DiGraph()
    with pytest.raises(ValueError, match="max_ring_size must be greater than or equal to min_ring_size"):
        find_wash_rings(graph, min_ring_size=5, max_ring_size=3)


def test_find_wash_rings_min_cycle_volume_filter():
    graph = nx.DiGraph()
    graph.add_edge("A", "B", total_volume=1.0, trade_count=1, timestamps=[])
    graph.add_edge("B", "C", total_volume=1.0, trade_count=1, timestamps=[])
    graph.add_edge("C", "A", total_volume=1.0, trade_count=1, timestamps=[])
    rings = find_wash_rings(graph, min_cycle_volume=100.0)
    assert len(rings) == 0


def test_graph_construction_and_ring_finding_performance_5k_50k():
    node_count = 5000
    edge_count = 50000
    nodes = [f"W{i:04d}" for i in range(node_count)]
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    rows = []

    for i in range(15):
        rows.append(
            {
                "ledger_close_time": base + _seconds(i),
                "base_account": nodes[i],
                "counter_account": nodes[(i + 1) % 15],
                "base_amount": 100.0,
            }
        )

    for i in range(node_count):
        rows.append(
            {
                "ledger_close_time": base,
                "base_account": nodes[i],
                "counter_account": nodes[i],
                "base_amount": 0.0,
            }
        )

    remaining = edge_count - len(rows)
    added = 0
    for src_idx in range(4985):
        for dst_idx in range(4985, node_count):
            if added >= remaining:
                break
            rows.append(
                {
                    "ledger_close_time": base,
                    "base_account": nodes[src_idx],
                    "counter_account": nodes[dst_idx],
                    "base_amount": 1.0,
                }
            )
            added += 1
        if added >= remaining:
            break

    trades = pd.DataFrame(rows)

    start = time.perf_counter()
    graph = build_transaction_graph(trades)
    rings = find_wash_rings(graph)
    elapsed = time.perf_counter() - start

    assert graph.number_of_nodes() == node_count
    assert graph.number_of_edges() == edge_count
    assert len(rings) == 1
    assert rings[0]["truncated"] is True
    assert elapsed < 2.0


def test_manual_graph_without_timestamps_still_finds_ring():
    graph = nx.DiGraph()
    graph.add_edge("A", "B", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("B", "C", total_volume=10.0, trade_count=1, timestamps=[])
    graph.add_edge("C", "A", total_volume=10.0, trade_count=1, timestamps=[])

    rings = find_wash_rings(graph)

    assert len(rings) == 1
    assert rings[0]["cycle_volume"] == 10.0
    assert rings[0]["timing_tightness"] == 0.0
