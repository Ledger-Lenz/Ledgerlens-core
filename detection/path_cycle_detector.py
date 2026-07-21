"""Multi-hop path-payment cycle detection.

`path_payment_engine.detect_atomic_circular_routes` flags a *single* atomic
path payment that round-trips within one transaction. It cannot see the more
sophisticated pattern this module targets: a wash ring spread across several
*separate* path payment operations, where funds leave an account, traverse a
handful of colluding sub-accounts and intermediary assets, and return to the
originator within a time window. No single hop looks anomalous and the legacy
counterparty graph (`graph_engine.build_transaction_graph`) records no direct
wash-trade edge — only the closed cycle reveals the self-dealing.

The detector expands the account graph with one directed edge per path payment
(`graph_engine.add_path_payment_edges`), then runs a time-windowed Johnson
simple-cycle search bounded to short rings (the economic incentive for wash
trading via path payments past ~6 hops is negligible).
"""

from __future__ import annotations

import logging

import networkx as nx
import pandas as pd

from detection.graph_engine import add_path_payment_edges
from ingestion.data_models import PathPayment

logger = logging.getLogger(__name__)

DEFAULT_MAX_CYCLE_LENGTH = 6
DEFAULT_MAX_TIME_WINDOW = pd.Timedelta(hours=24)
DEFAULT_MAX_COMPONENT_SIZE = 12


def path_payments_to_frame(payments: list[PathPayment]) -> pd.DataFrame:
    """Project `PathPayment` records into the column shape the graph expects."""
    rows = [
        {
            "source_account": p.source_account,
            "destination_account": p.destination_account,
            "source_asset": p.source_asset.pair_symbol,
            "destination_asset": p.destination_asset.pair_symbol,
            "source_amount": float(p.source_amount),
            "destination_amount": float(p.destination_amount),
            "timestamp": pd.Timestamp(p.timestamp),
            "transaction_hash": p.transaction_hash,
        }
        for p in payments
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "source_account",
            "destination_account",
            "source_asset",
            "destination_asset",
            "source_amount",
            "destination_amount",
            "timestamp",
            "transaction_hash",
        ],
    )


def build_path_payment_graph(payments: list[PathPayment] | pd.DataFrame) -> nx.DiGraph:
    """Build the directed path-payment account graph from raw payments."""
    frame = payments if isinstance(payments, pd.DataFrame) else path_payments_to_frame(payments)
    graph = nx.DiGraph()
    add_path_payment_edges(graph, frame)
    return graph


def _select_timed_cycle_edges(
    graph: nx.DiGraph,
    cycle: list[str],
    max_time_window: pd.Timedelta,
) -> list[dict] | None:
    """Pick one path payment per hop so the whole ring completes within a window.

    Funds flow around the ring cyclically, so there is no fixed "first" hop to
    anchor a monotonic ordering on — the only meaningful constraint is that the
    chosen payments all fall inside a `max_time_window`-wide span. When an edge
    carries several payments we choose the combination with the tightest span
    via the classic "smallest range covering one element from each list"
    sweep. Hops without a timestamp do not constrain the span (their first
    payment is taken).
    """
    timed: list[list[tuple[float, dict]]] = []  # one sorted list per timestamped edge
    untimed_picks: list[dict] = []
    for i in range(len(cycle)):
        source = cycle[i]
        destination = cycle[(i + 1) % len(cycle)]
        edge = graph.get_edge_data(source, destination)
        if not edge:
            return None
        hops = edge.get("path_payments", [])
        if not hops:
            return None
        with_ts = sorted(
            ((h["timestamp"].timestamp(), h) for h in hops if h["timestamp"] is not None),
            key=lambda pair: pair[0],
        )
        if with_ts:
            timed.append(with_ts)
        else:
            untimed_picks.append(hops[0])

    if not timed:
        return untimed_picks  # nothing to time-bound

    import heapq

    pointers = [0] * len(timed)
    heap = [(timed[i][0][0], i) for i in range(len(timed))]
    heapq.heapify(heap)
    current_max = max(lst[0][0] for lst in timed)

    best_range = float("inf")
    best_choice = list(pointers)
    while True:
        ts_min, li = heapq.heappop(heap)
        if current_max - ts_min < best_range:
            best_range = current_max - ts_min
            best_choice = list(pointers)
        nxt = pointers[li] + 1
        if nxt == len(timed[li]):
            break
        pointers[li] = nxt
        ts_next = timed[li][nxt][0]
        current_max = max(current_max, ts_next)
        heapq.heappush(heap, (ts_next, li))

    if best_range > max_time_window.total_seconds():
        return None

    selected = [timed[i][best_choice[i]][1] for i in range(len(timed))]
    selected.extend(untimed_picks)
    return selected


def detect_path_payment_cycles(
    graph: nx.DiGraph,
    root_accounts: set[str] | None = None,
    max_cycle_length: int = DEFAULT_MAX_CYCLE_LENGTH,
    max_time_window: pd.Timedelta = DEFAULT_MAX_TIME_WINDOW,
    min_cycle_xlm: float = 0.0,
    max_component_size: int = DEFAULT_MAX_COMPONENT_SIZE,
) -> list[dict]:
    """Return closed path-payment cycles over the expanded account graph.

    A cycle qualifies when it (1) starts and ends at the same account — and, if
    `root_accounts` is given, touches at least one of them (e.g. a known
    associate cluster from `graph_engine`); (2) completes within
    `max_time_window`; and (3) carries cyclic value (bottleneck hop volume) of
    at least `min_cycle_xlm`. Cycles are bounded to `max_cycle_length` hops via
    the `length_bound` argument to `simple_cycles`.

    A simple cycle can only exist within a single strongly connected
    component (SCC), so the search runs `simple_cycles` once per SCC instead
    of once over the whole graph — this bounds each call's search space to
    that component's node/edge count rather than the full graph, and lets
    components that don't touch `root_accounts` be skipped before any cycle
    enumeration happens. `simple_cycles` cost is combinatorial in a
    component's *density*, not just its size, even at a fixed
    `max_cycle_length` (real wash rings are exactly this shape: a small,
    near-complete cluster of colluding wallets); components larger than
    `max_component_size` are skipped with a warning rather than risking an
    unbounded enumeration. Real wash rings are small by construction (compare
    `graph_engine.find_wash_rings`'s `max_ring_size`, default 10), so this
    bound is not expected to affect realistic detections.
    """
    if max_cycle_length < 2:
        raise ValueError("max_cycle_length must be at least 2")

    cycles: list[dict] = []
    seen: set[frozenset[str]] = set()

    for component in nx.strongly_connected_components(graph):
        if len(component) < 2:
            continue
        if root_accounts is not None and not (component & root_accounts):
            continue
        if len(component) > max_component_size:
            logger.warning(
                "Skipping path-payment cycle search on a %d-node strongly "
                "connected component (exceeds max_component_size=%d); "
                "simple_cycles enumeration cost is combinatorial in "
                "component density",
                len(component),
                max_component_size,
            )
            continue

        subgraph = graph.subgraph(component)
        for node_cycle in nx.simple_cycles(subgraph, length_bound=max_cycle_length):
            if len(node_cycle) < 2:
                continue
            if root_accounts is not None and not (set(node_cycle) & root_accounts):
                continue

            selected = _select_timed_cycle_edges(subgraph, node_cycle, max_time_window)
            if selected is None:
                continue

            amounts = [hop["amount_xlm"] for hop in selected]
            cycle_value_xlm = float(min(amounts)) if amounts else 0.0
            if cycle_value_xlm < min_cycle_xlm:
                continue

            # Dedupe rotations of the same account set/length.
            signature = frozenset(node_cycle)
            if (signature, len(node_cycle)) in seen:
                continue
            seen.add((signature, len(node_cycle)))  # type: ignore[arg-type]

            cycle_path = [hop["source_asset"] for hop in selected]
            cycle_path.append(cycle_path[0])

            timestamps = [hop["timestamp"] for hop in selected if hop["timestamp"] is not None]
            if len(timestamps) >= 2:
                completed_in_seconds = float((max(timestamps) - min(timestamps)).total_seconds())
            else:
                completed_in_seconds = 0.0

            intermediate_assets = {
                hop["source_asset"] for hop in selected if hop["source_asset"]
            } | {hop["destination_asset"] for hop in selected if hop["destination_asset"]}

            cycles.append(
                {
                    "accounts": list(node_cycle),
                    "cycle_path": cycle_path,
                    "cycle_value_xlm": cycle_value_xlm,
                    "cycle_length": len(node_cycle),
                    "completed_in_seconds": completed_in_seconds,
                    "asset_diversity": len(intermediate_assets),
                    "transaction_hashes": [
                        hop["transaction_hash"] for hop in selected if hop["transaction_hash"]
                    ],
                }
            )

    return sorted(cycles, key=lambda c: c["cycle_value_xlm"], reverse=True)


def detect_cycles_from_payments(
    payments: list[PathPayment],
    root_accounts: set[str] | None = None,
    max_cycle_length: int = DEFAULT_MAX_CYCLE_LENGTH,
    max_time_window: pd.Timedelta = DEFAULT_MAX_TIME_WINDOW,
    min_cycle_xlm: float = 0.0,
    max_component_size: int = DEFAULT_MAX_COMPONENT_SIZE,
) -> list[dict]:
    """Convenience wrapper: build the graph and detect cycles in one call."""
    graph = build_path_payment_graph(payments)
    return detect_path_payment_cycles(
        graph,
        root_accounts=root_accounts,
        max_cycle_length=max_cycle_length,
        max_time_window=max_time_window,
        min_cycle_xlm=min_cycle_xlm,
        max_component_size=max_component_size,
    )


def path_cycle_features(
    cycles: list[dict] | None,
    account: str,
) -> dict:
    """Per-account cycle features for the ML feature vector.

    `cycles` is the output of `detect_path_payment_cycles` (computed once for
    the batch); features are restricted to cycles `account` participates in.
    """
    zero = {
        "path_cycle_count_24h": 0.0,
        "path_cycle_xlm_volume_24h": 0.0,
        "max_cycle_length": 0.0,
        "cycle_asset_diversity": 0.0,
    }
    if not cycles:
        return zero

    own = [c for c in cycles if account in c.get("accounts", [])]
    if not own:
        return zero

    assets: set[str] = set()
    for cycle in own:
        assets.update(a for a in cycle.get("cycle_path", []) if a)

    return {
        "path_cycle_count_24h": float(len(own)),
        "path_cycle_xlm_volume_24h": float(sum(c["cycle_value_xlm"] for c in own)),
        "max_cycle_length": float(max(c["cycle_length"] for c in own)),
        "cycle_asset_diversity": float(len(assets)),
    }


def path_payment_cycles_to_alerts(cycles: list[dict]) -> list[dict]:
    """Convert detected cycles into `PATH_PAYMENT_CYCLE` alert dicts.

    Each cycle is attributed to its first (originating) account as `wallet`; the
    full account ring, asset path, cyclic value and completion time travel in
    the alert `detail`. The asset pair is the first->last leg of the ring.
    """
    from detection.storage import AlertType

    alerts: list[dict] = []
    for cycle in cycles:
        accounts = cycle.get("accounts", [])
        if not accounts:
            continue
        cycle_path = cycle.get("cycle_path", [])
        asset_pair = (
            f"{cycle_path[0]}/{cycle_path[-1]}" if len(cycle_path) >= 2 else "UNKNOWN"
        )
        alerts.append(
            {
                "alert_type": AlertType.PATH_PAYMENT_CYCLE.value,
                "wallet": accounts[0],
                "asset_pair": asset_pair,
                "detail": {
                    "accounts": accounts,
                    "cycle_path": cycle_path,
                    "cycle_value_xlm": cycle["cycle_value_xlm"],
                    "completed_in_seconds": cycle["completed_in_seconds"],
                    "cycle_length": cycle["cycle_length"],
                    "asset_diversity": cycle.get("asset_diversity", 0),
                    "transaction_hashes": cycle.get("transaction_hashes", []),
                },
            }
        )
    return alerts
