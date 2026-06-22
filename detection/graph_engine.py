"""Graph-based wash-ring discovery for SDEX trade flows."""

import logging
import statistics
from typing import Any

import networkx as nx
import pandas as pd

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
