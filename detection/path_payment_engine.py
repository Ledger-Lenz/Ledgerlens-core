"""Atomic path-payment circularity detection and multi-hop wash-trade detection.

A single signed transaction can route `XLM -> A -> B -> XLM` through several
order books and/or pools in one atomic operation. That ingests as a sequence
of unrelated `Trade` rows with no link back to the parent transaction, so a
wallet that round-trips its own funds through a multi-hop path payment is
invisible to single-account, consecutive-trade detectors. This module flags
that pattern directly from `ingestion.data_models.PathPayment` records.

PathPaymentGraph / PathCycleDetector implement the multi-hop engine described
in GitHub issue #121: a directed (wallet, asset) hop graph with iterative DFS
cycle detection bounded to 7 hops.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from ingestion.data_models import PathPayment

logger = logging.getLogger(__name__)

# ── Validation patterns ─────────────────────────────────────────────────────
_ASSET_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")
_STELLAR_KEY_RE = re.compile(r"^G[A-Z2-7]{55}$")

# ── Safety caps ──────────────────────────────────────────────────────────────
MAX_NODES_PER_WALLET = 500
MAX_EDGES_PER_WALLET = 2000
MAX_GRAPH_EDGES = 500_000

# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class HopEdge:
    """A single asset-hop transfer within a path payment operation."""

    src_wallet: str
    src_asset: str
    dst_wallet: str
    dst_asset: str
    amount: float
    ledger_timestamp: datetime
    operation_id: str


@dataclass
class PathPaymentCycle:
    """A detected round-trip cycle across 3–7 path payment hops."""

    origin_wallet: str
    origin_asset: str
    hops: list[HopEdge]
    recovery_ratio: float
    cycle_duration_seconds: float
    counterparty_overlap: float
    cycle_score: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def path_length(self) -> int:
        return len(self.hops)


# ── Scoring ──────────────────────────────────────────────────────────────────


def _score_cycle(cycle: PathPaymentCycle) -> float:
    """Composite score: 40% recovery + 30% timing + 20% length + 10% overlap."""
    timing_score = 1.0 / (1.0 + cycle.cycle_duration_seconds / 600.0)
    length_score = 1.0 - (1.0 / max(cycle.path_length, 1))
    return (
        0.40 * cycle.recovery_ratio
        + 0.30 * timing_score
        + 0.20 * length_score
        + 0.10 * cycle.counterparty_overlap
    )


# ── Graph ────────────────────────────────────────────────────────────────────


def _validate_asset(asset_code: str, asset_issuer: str | None) -> bool:
    if not _ASSET_CODE_RE.match(asset_code):
        return False
    if asset_issuer is not None and not _STELLAR_KEY_RE.match(asset_issuer):
        return False
    return True


def _validate_wallet(wallet: str) -> bool:
    return bool(_STELLAR_KEY_RE.match(wallet))


class PathPaymentGraph:
    """Directed (wallet, asset) hop graph for multi-hop wash-trade detection.

    Nodes are ``(wallet, asset)`` tuples. Edges are individual HopEdge records
    keyed by source-node → dest-node with a list of edges (there may be many
    hops between the same node pair within the time window).

    Edges older than ``cycle_window_seconds`` are pruned on each ``add_hop``
    call to bound memory. A global ``MAX_GRAPH_EDGES`` limit evicts the oldest
    edges when the graph grows too large.
    """

    def __init__(self, cycle_window_seconds: float = 3600.0, max_depth: int = 7) -> None:
        self.cycle_window_seconds = cycle_window_seconds
        self.max_depth = max_depth
        # adjacency: src_node -> {dst_node -> [HopEdge, ...]}
        self._adj: dict[tuple, dict[tuple, list[HopEdge]]] = defaultdict(lambda: defaultdict(list))
        # track total edge count
        self._total_edges: int = 0
        # dedup set
        self._seen_ops: set[str] = set()
        # per-wallet node/edge counts for safety caps
        self._wallet_nodes: dict[str, set[tuple]] = defaultdict(set)
        self._wallet_edges: dict[str, int] = defaultdict(int)

    def _prune_old_edges(self, now_ts: float) -> None:
        cutoff = now_ts - self.cycle_window_seconds
        to_delete_src: list[tuple] = []
        for src_node, dst_map in self._adj.items():
            to_delete_dst: list[tuple] = []
            for dst_node, edges in dst_map.items():
                fresh = [e for e in edges if e.ledger_timestamp.timestamp() >= cutoff]
                removed = len(edges) - len(fresh)
                if removed:
                    self._total_edges -= removed
                    wallet = src_node[0]
                    self._wallet_edges[wallet] = max(0, self._wallet_edges[wallet] - removed)
                if fresh:
                    dst_map[dst_node] = fresh
                else:
                    to_delete_dst.append(dst_node)
            for dst_node in to_delete_dst:
                del dst_map[dst_node]
                wallet = src_node[0]
                self._wallet_nodes[wallet].discard(src_node)
            if not dst_map:
                to_delete_src.append(src_node)
        for src_node in to_delete_src:
            del self._adj[src_node]

    def _evict_oldest(self) -> None:
        """Evict the globally oldest edge when MAX_GRAPH_EDGES is exceeded."""
        oldest_ts = math.inf
        oldest_src: tuple | None = None
        oldest_dst: tuple | None = None
        for src_node, dst_map in self._adj.items():
            for dst_node, edges in dst_map.items():
                if edges and edges[0].ledger_timestamp.timestamp() < oldest_ts:
                    oldest_ts = edges[0].ledger_timestamp.timestamp()
                    oldest_src = src_node
                    oldest_dst = dst_node
        if oldest_src and oldest_dst:
            edges = self._adj[oldest_src][oldest_dst]
            if edges:
                edges.pop(0)
                self._total_edges -= 1
                wallet = oldest_src[0]
                self._wallet_edges[wallet] = max(0, self._wallet_edges[wallet] - 1)
                if not edges:
                    del self._adj[oldest_src][oldest_dst]
                    self._wallet_nodes[wallet].discard(oldest_src)

    def add_hop(self, edge: HopEdge) -> None:
        """Add a single hop edge, deduplicating on operation_id and pruning stale edges."""
        if edge.operation_id in self._seen_ops:
            return
        self._seen_ops.add(edge.operation_id)

        now_ts = edge.ledger_timestamp.timestamp()
        self._prune_old_edges(now_ts)

        src_node = (edge.src_wallet, edge.src_asset)
        dst_node = (edge.dst_wallet, edge.dst_asset)
        wallet = edge.src_wallet

        # Per-wallet caps
        self._wallet_nodes[wallet].add(src_node)
        if len(self._wallet_nodes[wallet]) > MAX_NODES_PER_WALLET:
            logger.warning(
                "PathPaymentGraph: wallet %s exceeded MAX_NODES_PER_WALLET=%d; DFS skipped",
                wallet,
                MAX_NODES_PER_WALLET,
            )
            return
        if self._wallet_edges[wallet] >= MAX_EDGES_PER_WALLET:
            logger.warning(
                "PathPaymentGraph: wallet %s exceeded MAX_EDGES_PER_WALLET=%d; DFS skipped",
                wallet,
                MAX_EDGES_PER_WALLET,
            )
            return

        # Global cap
        if self._total_edges >= MAX_GRAPH_EDGES:
            self._evict_oldest()

        self._adj[src_node][dst_node].append(edge)
        self._total_edges += 1
        self._wallet_edges[wallet] += 1

    def find_cycles(self, origin_wallet: str) -> list[PathPaymentCycle]:
        """DFS from all (origin_wallet, asset) nodes to find round-trip cycles."""
        if self._wallet_edges.get(origin_wallet, 0) == 0:
            return []
        if self._wallet_nodes.get(origin_wallet) and len(self._wallet_nodes[origin_wallet]) > MAX_NODES_PER_WALLET:
            return []

        origin_nodes = [n for n in self._adj if n[0] == origin_wallet]
        cycles: list[PathPaymentCycle] = []
        seen_cycle_ids: set[frozenset] = set()

        for start_node in origin_nodes:
            self._dfs(start_node, start_node, [], cycles, seen_cycle_ids)

        return cycles

    def _dfs(
        self,
        start_node: tuple,
        current_node: tuple,
        path: list[HopEdge],
        results: list[PathPaymentCycle],
        seen: set[frozenset],
    ) -> None:
        if len(path) > self.max_depth:
            return

        for dst_node, edges in self._adj.get(current_node, {}).items():
            if not edges:
                continue
            edge = edges[-1]  # use most recent hop for cycle construction

            # Cycle detected: dst_node is origin wallet (same wallet, same asset)
            if dst_node == start_node and len(path) >= 2:
                full_hops = path + [edge]
                cycle_id = frozenset(id(e) for e in full_hops)
                if cycle_id in seen:
                    continue
                seen.add(cycle_id)

                amounts_sent = full_hops[0].amount
                amounts_recv = full_hops[-1].amount
                recovery_ratio = amounts_recv / amounts_sent if amounts_sent > 0 else 0.0
                if recovery_ratio < 0.5:
                    continue

                timestamps = sorted(e.ledger_timestamp for e in full_hops)
                duration = (timestamps[-1] - timestamps[0]).total_seconds()

                wallets = [e.src_wallet for e in full_hops]
                total_hops = len(full_hops)
                wallet_counts: dict[str, int] = {}
                for w in wallets:
                    wallet_counts[w] = wallet_counts.get(w, 0) + 1
                max_count = max(wallet_counts.values()) if wallet_counts else 0
                counterparty_overlap = (max_count - 1) / (total_hops - 1) if total_hops > 1 else 0.0

                cycle = PathPaymentCycle(
                    origin_wallet=start_node[0],
                    origin_asset=start_node[1],
                    hops=full_hops,
                    recovery_ratio=recovery_ratio,
                    cycle_duration_seconds=duration,
                    counterparty_overlap=counterparty_overlap,
                    cycle_score=0.0,
                )
                cycle.cycle_score = _score_cycle(cycle)
                results.append(cycle)
                continue

            # Avoid revisiting nodes in current path (no repeated intermediate nodes)
            if dst_node in {start_node} | {(e.src_wallet, e.src_asset) for e in path}:
                continue
            if len(path) >= self.max_depth:
                continue

            self._dfs(start_node, dst_node, path + [edge], results, seen)


# ── Detector ─────────────────────────────────────────────────────────────────


class PathCycleDetector:
    """High-level detector: ingest Horizon hop records, emit PathPaymentCycle alerts.

    Wraps PathPaymentGraph with configuration thresholds, storage, and
    per-wallet feature extraction.
    """

    def __init__(
        self,
        cycle_window_seconds: float = 3600.0,
        max_depth: int = 7,
        min_recovery_ratio: float = 0.95,
        min_cycle_score: float = 0.6,
    ) -> None:
        _validate_window(cycle_window_seconds)
        self.min_recovery_ratio = min_recovery_ratio
        self.min_cycle_score = min_cycle_score
        self._graph = PathPaymentGraph(
            cycle_window_seconds=cycle_window_seconds, max_depth=max_depth
        )
        # wallet -> list of confirmed cycles
        self._cycles: dict[str, list[PathPaymentCycle]] = defaultdict(list)

    def ingest(self, hop_records: list[dict]) -> list[PathPaymentCycle]:
        """Process raw Horizon path_payment records and return newly detected cycles."""
        newly_detected: list[PathPaymentCycle] = []
        wallets_to_check: set[str] = set()

        for rec in hop_records:
            edge = _record_to_hop_edge(rec)
            if edge is None:
                continue
            self._graph.add_hop(edge)
            wallets_to_check.add(edge.src_wallet)

        for wallet in wallets_to_check:
            for cycle in self._graph.find_cycles(wallet):
                if cycle.recovery_ratio < self.min_recovery_ratio:
                    continue
                if cycle.cycle_score < self.min_cycle_score:
                    continue
                self._cycles[wallet].append(cycle)
                newly_detected.append(cycle)

        return newly_detected

    def get_features(self, wallet: str) -> dict[str, float]:
        """Return ML features for the given wallet."""
        cycles = self._cycles.get(wallet, [])
        if not cycles:
            return {"path_cycle_count": 0.0, "path_cycle_recovery_ratio": 0.0}
        return {
            "path_cycle_count": float(len(cycles)),
            "path_cycle_recovery_ratio": max(c.recovery_ratio for c in cycles),
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate_window(seconds: float) -> None:
    if not (300 <= seconds <= 86400):
        raise ValueError(
            f"cycle_window_seconds must be between 300 and 86400, got {seconds}"
        )


def _record_to_hop_edge(rec: dict) -> HopEdge | None:
    """Convert a raw Horizon operation dict to a HopEdge, or return None if invalid."""
    try:
        src_wallet = rec.get("source_account", "")
        dst_wallet = rec.get("to", rec.get("destination_account", ""))
        src_asset_code = rec.get("asset_code", rec.get("sending_asset_code", "XLM"))
        dst_asset_code = rec.get("destination_asset_code", rec.get("receiving_asset_code", "XLM"))
        src_asset_issuer = rec.get("asset_issuer", rec.get("sending_asset_issuer"))
        dst_asset_issuer = rec.get("destination_asset_issuer", rec.get("receiving_asset_issuer"))
        amount = float(rec.get("amount", rec.get("source_amount", 0.0)))
        operation_id = str(rec.get("id", rec.get("operation_id", "")))

        ts_raw = rec.get("created_at", rec.get("ledger_timestamp"))
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            return None

        if not _validate_wallet(src_wallet):
            return None
        if not _validate_wallet(dst_wallet):
            return None
        if not _validate_asset(src_asset_code, src_asset_issuer):
            return None
        if not _validate_asset(dst_asset_code, dst_asset_issuer):
            return None
        if not operation_id:
            return None

        return HopEdge(
            src_wallet=src_wallet,
            src_asset=src_asset_code,
            dst_wallet=dst_wallet,
            dst_asset=dst_asset_code,
            amount=amount,
            ledger_timestamp=ts,
            operation_id=operation_id,
        )
    except Exception:
        return None


def detect_atomic_circular_routes(path_payments: list[PathPayment]) -> list[dict]:
    """Flag path payments where:

    - `source_account == destination_account` (atomic self-payment loop), or
    - `destination_asset == source_asset` (round-trips back to the same asset
      even when `destination_account` differs — still manufactures volume
      with no net economic position change).

    A legitimate non-cyclic multi-hop payment to a different destination in a
    different asset is not flagged.
    """
    routes = []
    for payment in path_payments:
        is_self_payment = payment.source_account == payment.destination_account
        is_same_asset_cycle = payment.source_asset.pair_symbol == payment.destination_asset.pair_symbol
        if not is_self_payment and not is_same_asset_cycle:
            continue

        routes.append(
            {
                "transaction_hash": payment.transaction_hash,
                "accounts": sorted({payment.source_account, payment.destination_account}),
                "hop_count": len(payment.path) + 1,
                "cycle_volume": min(payment.source_amount, payment.destination_amount),
                "is_atomic_self_payment": is_self_payment,
                "touches_pool": False,
            }
        )
    return routes


def analyze_path_payments(
    path_payments: list[PathPayment],
    root_accounts: set[str] | None = None,
    max_cycle_length: int = 6,
    max_time_window: pd.Timedelta = pd.Timedelta(hours=24),
    min_cycle_xlm: float = 0.0,
) -> dict:
    """Run both detectors over a batch of path payments.

    Returns the per-transaction atomic circular routes (the legacy single-op
    pattern) alongside the multi-hop cross-operation cycles surfaced by
    `path_cycle_detector`. Keeping the cycle search behind this single entry
    point lets `run_pipeline` build the graph once per batch rather than per
    account.
    """
    from detection.path_cycle_detector import detect_cycles_from_payments

    return {
        "atomic_routes": detect_atomic_circular_routes(path_payments),
        "cycles": detect_cycles_from_payments(
            path_payments,
            root_accounts=root_accounts,
            max_cycle_length=max_cycle_length,
            max_time_window=max_time_window,
            min_cycle_xlm=min_cycle_xlm,
        ),
    }
