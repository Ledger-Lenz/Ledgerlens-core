#!/usr/bin/env python3
"""Capacity benchmark for ``detection.vector_index.FaissVectorIndex``.

Measures, for each backend at a series of wallet counts up to 10x the
current production scale (50 000 wallets, the default
``vector_index_ivf_threshold``):

- index build time (``add_batch`` of all vectors, including IVF training),
- index memory footprint (serialized FAISS index + wallet id bookkeeping),
- single-query ``search(k=10)`` latency p50/p99,
- recall@10 of the approximate backend against exact ``faiss_flat`` results.

Vectors are synthetic, clustered embeddings (seeded) so IVF behaves as it
would on real, non-uniform wallet embeddings. Results are printed as a
Markdown table; see ``docs/vector_index_capacity.md`` for recorded figures.

Usage
-----
    python3 benchmarks/benchmark_vector_index.py
    python3 benchmarks/benchmark_vector_index.py --scales 50000 500000 --queries 200
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import faiss

from detection.vector_index import FaissVectorIndex

CURRENT_SCALE = 50_000
DEFAULT_SCALES = [CURRENT_SCALE, 100_000, 250_000, 500_000]
DIM = 64
K = 10
SEED = 42


def synthetic_embeddings(n: int, dim: int, rng: np.random.Generator) -> np.ndarray:
    """Clustered Gaussian embeddings (~1 cluster per 500 wallets)."""
    n_centers = max(n // 500, 10)
    centers = rng.normal(size=(n_centers, dim)).astype(np.float32)
    labels = rng.integers(0, n_centers, size=n)
    return centers[labels] + 0.3 * rng.normal(size=(n, dim)).astype(np.float32)


def index_bytes(index: FaissVectorIndex) -> int:
    faiss_bytes = faiss.serialize_index(index._index).nbytes
    id_bytes = sum(sys.getsizeof(w) for w in index._wallet_list)
    return faiss_bytes + id_bytes + sys.getsizeof(index._wallet_to_idx)


def run(backend: str, vectors: np.ndarray, wallets: list[str], queries: np.ndarray):
    index = FaissVectorIndex(dim=DIM, backend=backend, ivf_threshold=len(wallets) + 1)
    start = time.perf_counter()
    index.add_batch(wallets, vectors)
    build_s = time.perf_counter() - start

    latencies = []
    results = []
    for q in queries:
        t0 = time.perf_counter()
        results.append(index.search(q, K))
        latencies.append((time.perf_counter() - t0) * 1000)
    latencies.sort()
    p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))]
    return {
        "build_s": build_s,
        "mem_mb": index_bytes(index) / 1e6,
        "p50_ms": statistics.median(latencies),
        "p99_ms": p99,
        "results": results,
    }


def recall(approx: list, exact: list) -> float:
    hits = sum(len({w for w, _ in a} & {w for w, _ in e}) for a, e in zip(approx, exact))
    return hits / (len(exact) * K)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scales", type=int, nargs="+", default=DEFAULT_SCALES)
    parser.add_argument("--queries", type=int, default=500)
    args = parser.parse_args()

    print(f"faiss {faiss.__version__}, dim={DIM}, k={K}, queries={args.queries}\n")
    print("| Wallets | Backend | Build (s) | Memory (MB) | p50 (ms) | p99 (ms) | Recall@10 |")
    print("|--------:|---------|----------:|------------:|---------:|---------:|----------:|")
    for n in args.scales:
        rng = np.random.default_rng(SEED)
        vectors = synthetic_embeddings(n, DIM, rng)
        wallets = [f"G{i:055d}" for i in range(n)]
        queries = vectors[rng.integers(0, n, size=args.queries)] + 0.05 * rng.normal(
            size=(args.queries, DIM)
        ).astype(np.float32)

        flat = run("faiss_flat", vectors.copy(), wallets, queries)
        ivf = run("faiss_ivf", vectors.copy(), wallets, queries)
        for name, r in (("faiss_flat", flat), ("faiss_ivf", ivf)):
            rec = 1.0 if name == "faiss_flat" else recall(r["results"], flat["results"])
            print(
                f"| {n:,} | {name} | {r['build_s']:.2f} | {r['mem_mb']:.1f} "
                f"| {r['p50_ms']:.2f} | {r['p99_ms']:.2f} | {rec:.3f} |"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
