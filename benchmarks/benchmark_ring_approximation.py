#!/usr/bin/env python3
"""Accuracy/latency trade-off of approximate vs. exact wash-ring detection.

Builds a seeded, labeled graph: ``--rings`` planted wash rings (member
wallets labeled positive) embedded in a sparse random background of
``--background`` wallets (labeled negative). Both modes of
``detection.graph_engine.find_wash_rings`` are run and wallet-level
precision/recall plus wall-clock time are reported, along with the delta.

Usage
-----
    python3 benchmarks/benchmark_ring_approximation.py
    python3 benchmarks/benchmark_ring_approximation.py --budget 5000 --seed 7
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import networkx as nx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detection.graph_engine import find_wash_rings


def build_labeled_graph(rings: int, ring_size: int, background: int, seed: int):
    rng = np.random.default_rng(seed)
    graph = nx.DiGraph()
    positives: set[str] = set()
    for r in range(rings):
        members = [f"ring{r}_{i}" for i in range(ring_size)]
        positives.update(members)
        for i, src in enumerate(members):
            graph.add_edge(
                src, members[(i + 1) % ring_size], volume=1000.0, trade_count=5, timestamps=[]
            )
    noise = [f"w{i}" for i in range(background)]
    graph.add_nodes_from(noise)
    # Acyclic background (edges only go from lower to higher index) plus
    # bridges into rings, so the only true cycles are the planted rings.
    for i in range(background):
        for j in rng.integers(i + 1, background + 1, size=2):
            if j < background:
                graph.add_edge(noise[i], noise[j], volume=10.0, trade_count=1, timestamps=[])
        if rng.random() < 0.01:
            graph.add_edge(
                noise[i], f"ring{rng.integers(rings)}_0", volume=10.0, trade_count=1, timestamps=[]
            )
    return graph, positives


def score(rings: list[dict], positives: set[str]) -> tuple[float, float]:
    flagged = {a for ring in rings for a in ring["accounts"]}
    tp = len(flagged & positives)
    precision = tp / len(flagged) if flagged else 1.0
    recall = tp / len(positives) if positives else 1.0
    return precision, recall


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rings", type=int, default=200)
    parser.add_argument("--ring-size", type=int, default=4)
    parser.add_argument("--background", type=int, default=50_000)
    parser.add_argument(
        "--budget", type=int, default=10_000, help="sample node budget in approximation mode"
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    graph, positives = build_labeled_graph(args.rings, args.ring_size, args.background, args.seed)
    print(
        f"graph: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges, {len(positives)} positives"
    )

    results = {}
    for mode, threshold in (("exact", None), ("approximate", 0)):
        start = time.perf_counter()
        rings = (
            find_wash_rings(
                graph,
                approximation_threshold=threshold,
                sample_node_budget=args.budget,
                seed=args.seed,
            )
            if threshold is not None
            else find_wash_rings(graph, approximation_threshold=graph.number_of_nodes())
        )
        elapsed = time.perf_counter() - start
        precision, recall = score(rings, positives)
        results[mode] = (precision, recall, elapsed)
        print(
            f"{mode:>11}: precision={precision:.3f} recall={recall:.3f} time={elapsed:.2f}s rings={len(rings)}"
        )

    ex, ap = results["exact"], results["approximate"]
    print(
        f"      delta: precision={ap[0] - ex[0]:+.3f} recall={ap[1] - ex[1]:+.3f} speedup={ex[2] / ap[2]:.1f}x"
    )


if __name__ == "__main__":
    main()
