# Graph Engine Performance

Profiling results for `detection/graph_engine.py` Tarjan SCC ring detector on a single CPU core (Linux, Python 3.12, Intel Core i7 class hardware).

## Methodology

Benchmarks were run with `python3 -m pytest tests/test_iterative_tarjan.py::test_performance_100k_nodes_500k_edges -s` on a synthetic random directed graph generated with `numpy.random.default_rng(42).integers(0, n_nodes, n_edges)` for uniformly-distributed source/destination pairs.

Time is measured with `time.perf_counter()`. Peak RAM is measured with `tracemalloc` scoped to the `find_wash_rings()` call only (excludes trade ingestion, which is dominated by dict/string allocations rather than the graph data structures).

## Results

| Implementation         | Nodes  | Edges   | Ingest (s) | Ring-find (s) | Total (s) | Peak RAM (MB) |
| ---------------------- | ------ | ------- | ---------- | ------------- | --------- | ------------- |
| Recursive (networkx)   | 10 K   | 50 K    | 0.2        | 0.5           | 0.7       | ~25           |
| Iterative (dict adj.)  | 10 K   | 50 K    | 0.2        | 0.3           | 0.5       | ~15           |
| Iterative + CSR        | 100 K  | 500 K   | 3.3        | 12.2          | ~15.4     | 62.5          |

Target: 100 K nodes, 500 K edges in **< 30 s** on a single CPU core with **< 500 MB** peak RAM.

**This benchmark does not exercise cycle-volume computation.** It uses uniformly-random
edge placement (`numpy.random.default_rng(42).integers(...)`), which produces one giant
SCC covering ~98% of nodes (truncated, since it exceeds `max_ring_size=10` — see "Ring
metric computation" below) plus many singleton SCCs (filtered out by `min_ring_size=3`).
Neither case reaches `_cycle_volume`. Real wash-trading rings are small, dense,
near-complete clusters of colluding wallets — the opposite shape from this benchmark's
input — so a separate benchmark below measures that path directly.

## Cycle-volume computation on dense rings

`_cycle_volume` computes the maximum bottleneck-weight (minimum edge `total_volume`)
simple cycle within a detected ring, for every non-truncated ring (`min_ring_size <=
len(accounts) <= max_ring_size`). It is exercised on every real wash-ring detection —
unlike the SCC-finding step above, its cost does not depend on graph size but on the
*density* of each individual ring, since the number of simple cycles in a subgraph
grows combinatorially with edge density. A 10-node, ~90%-density subgraph (a typical
"bot farm" wash-ring: every wallet trading with every other wallet) has 576 855 simple
cycles of length ≥ 3.

`_cycle_volume` is computed via a bitmask DP (`_max_bottleneck_cycle`, adapted from the
maximum-bottleneck-path technique) instead of enumerating cycles with
`networkx.simple_cycles`: its cost is `O(n² · 2ⁿ)`, a function of node count alone,
independent of edge density or cycle count. It is exact, not an approximation, for any
subgraph up to `_MAX_EXACT_CYCLE_VOLUME_NODES = 14` nodes (well above the default
`max_ring_size = 10`); beyond that it falls back to a documented, bounded-error
threshold-search approximation (see `_approx_max_bottleneck_cycle`'s docstring) that is
not exercised at default configuration.

**Methodology:** `tests/test_iterative_tarjan.py::test_performance_dense_ring_cycle_volume`
builds a single 10-node SCC (`max_ring_size` default) at ~90% directed-edge density with
randomised per-edge volumes — a directed Hamiltonian ring plus random chords, guaranteeing
strong connectivity regardless of which additional edges land — and runs it through
`TradeGraph.find_wash_rings()`.

| Metric                                          | Old (`nx.simple_cycles` enumeration) | New (bitmask DP) |
| ------------------------------------------------ | ------------------------------------- | ----------------- |
| 10-node, ~90%-density ring, cycle-volume time     | 5.33 s (576 855 cycles enumerated)    | **10.9 ms**        |
| Speedup                                           | —                                      | **~490×**          |
| Result                                            | identical (929.9)                     | identical (929.9)  |

Enforced budget (CI-failing): **< 1 s** per ring at `max_ring_size` and worst-case
(near-complete) density — measured result is ~100× under budget. The old implementation
would already exceed a 1 s budget on a single ring at this shape, and its cost grows
combinatorially worse with size/density beyond it (a related 10-node case has been
measured at over 1.1 million simple cycles / ~4 s for enumeration alone); the new
implementation's cost is bounded by node count regardless of density.

## Notes

### Iterative Tarjan (`IterativeTarjanSCC`)

Replaces networkx's recursive Tarjan with an explicit work-stack. This eliminates Python's default recursion limit of ~1 000 frames which previously caused `RecursionError` for graphs with more than ~1 000 nodes in a single strongly-connected component.

Time complexity: O(V + E). For a 100 K-node, 500 K-edge graph: approximately 1.2 s for the Tarjan traversal itself.

### CSR Adjacency (`SparseTradeGraph`)

For graphs with `n_nodes >= GRAPH_MMAP_THRESHOLD` (default 50 000), the adjacency list is stored as a `scipy.sparse.csr_matrix`. Building the CSR matrix from aggregated edge data takes approximately 3.5 s for 500 K edges; `to_adjacency_dict()` then takes ~0.1 s.

Memory for the CSR matrix at 500 K edges: ~4 MB data (float64) + ~1 MB indices (int32). The `lil_matrix` used during construction has higher transient memory but is freed immediately after `csr_matrix()` conversion.

### Ring metric computation

For the typical 100 K-node random graph, a single giant SCC containing ~98 % of all nodes is detected and flagged as truncated (exceeds `max_ring_size=10`). Metrics (total volume, timing tightness, avg trade count) are computed with a single O(E) pass over the aggregated edge-data dict, avoiding the overhead of building a full `networkx.DiGraph` for large SCCs. The remaining SCCs are singletons and are skipped by the `min_ring_size=3` filter.

### Path-payment cycle detection (`detection/path_cycle_detector.py`)

`detect_path_payment_cycles` uses the same `nx.simple_cycles` call pattern as the
pre-fix `_cycle_volume` (bounded to `max_cycle_length` hops, default 6), but needs the
actual cycle instances — not just a bottleneck scalar — to select per-hop timestamps and
build alert detail, so it is not a candidate for the bitmask-DP replacement above.
Instead, cycle search is now scoped to one strongly connected component at a time
(`nx.strongly_connected_components`) rather than run directly over the whole
path-payment graph: a simple cycle can only exist within one SCC, so this bounds each
`simple_cycles` call's search space to that component's size and lets components that
don't touch `root_accounts` be skipped before any enumeration happens. Components larger
than `max_component_size` (default 12) are skipped with a warning, since
`simple_cycles`'s cost is combinatorial in component *density* even at a fixed
`max_cycle_length` — the same underlying risk `_cycle_volume` had, applied to whole
graph instead of a pre-isolated ring. Real wash rings are small by construction
(`graph_engine.find_wash_rings`'s `max_ring_size`, default 10), so this bound does not
affect realistic detections; `tests/test_path_cycle_detector.py` covers the 10 000-op
mixed-cycle/acyclic case in well under its 10 s budget.

### Threshold settings

| Variable              | Default  | Description                                                     |
| --------------------- | -------- | --------------------------------------------------------------- |
| `GRAPH_MMAP_THRESHOLD`| 50 000   | Node count above which CSR adjacency is used instead of a dict  |
| `MAX_GRAPH_NODES`     | 1 000 000| Hard cap; `GraphTooLargeError` is raised above this limit       |


## Sharded Graph Engine

When the node count would exceed `MAX_GRAPH_NODES` (default 1,000,000), an
adaptive sharded graph engine can be activated instead of raising
`GraphTooLargeError`. This is controlled by `GRAPH_SHARD_ENABLED` (default `true`).

### How it works

1. **Community-detection partitioning**: The full node set is processed by
   `GraphShardPartitioner` (in `detection/graph_sharding.py`) which runs Louvain
   community detection on the undirected projection of the trade graph. Densely
   connected clusters (wash rings) are assigned to the same shard, keeping most
   rings intact within a single partition.

2. **Boundary-overlap buffer**: Accounts within `GRAPH_SHARD_OVERLAP_HOPS` hops
   (default 1) of a shard boundary are replicated into neighbouring shards. This
   ensures that cycles crossing the boundary by a small number of hops are still
   detected in at least one shard.

3. **Parallel per-shard SCC**: Each shard runs `TradeGraph.find_wash_rings`
   independently via a `multiprocessing.Pool` (size = `GRAPH_SHARD_MAX_WORKERS`).
   Results are merged with de-duplication — rings whose account set is a subset
   of an already-seen ring from another shard (via a replicated boundary node)
   keep only the larger/higher-volume ring.

### Accuracy tradeoff at boundaries

A ring whose accounts are split across a shard boundary **beyond** the overlap-hop
buffer will **not** be detected as a complete ring. Each shard sees only its
partial view of the ring, and no individual shard's SCC detection will find the
full cycle.

**Worked example**: Consider a 5-node wash ring `{A, B, C, D, E}` where
partitioning assigns `{A, B, C}` to shard 0 and `{D, E}` to shard 1, with
`GRAPH_SHARD_OVERLAP_HOPS=1`. If the only cross-boundary edge is `C -> D` (1 hop
from the boundary), then `D` is replicated into shard 0 and the full ring is
detected there. If instead the cross-boundary edges are `A -> D` and neither
side has an edge within 1 hop of the boundary, then neither shard sees the full
ring: shard 0 sees `{A, B, C}` as a 3-node SCC, and shard 1 sees `{D, E}` as
a 2-node SCC (filtered by `min_ring_size=3`). This is a documented, accepted
limitation: wash rings are, by construction, locally dense (tight cycles of a
handful to a few dozen accounts trading repeatedly with each other), so
community-detection partitioning keeps most rings intact within a single shard.

### Configuration

| Variable                     | Default  | Description                                                        |
| ---------------------------- | -------- | ------------------------------------------------------------------ |
| `GRAPH_SHARD_ENABLED`        | true     | Auto-route to sharding when `MAX_GRAPH_NODES` would be exceeded    |
| `GRAPH_SHARD_COUNT`          | 8        | Number of partitions; each targets `MAX_GRAPH_NODES // count`      |
| `GRAPH_SHARD_OVERLAP_HOPS`   | 1        | Hop-distance boundary replication buffer (0-3)                     |
| `GRAPH_SHARD_MAX_WORKERS`    | 8        | `multiprocessing.Pool` size for per-shard SCC computation          |


## Numba JIT for feature engineering

`round_trip_trade_frequency` and `cross_pair_features` in `detection/feature_engineering.py`
use Numba `@njit`-compiled kernels for their hot inner loops (reversed-leg comparison
and pairwise burst-window timestamp matching), falling back to pure Python when
Numba is unavailable or disabled.

**Flag:** `FEATURE_ENGINE_JIT_ENABLED` (default `true`). Set to `false` to force
the pure-Python path — useful for cold-start-sensitive serverless deployments,
since Numba's first-call compilation adds measurable warm-up latency.

**Benchmark results** (run via `python benchmarks/benchmark_feature_engineering.py`):

<paste your actual benchmark output here>

**When to disable:** if your deployment has strict cold-start latency requirements
(e.g. AWS Lambda, short-lived containers) and doesn't reuse warm processes long
enough to amortize JIT compilation cost, set the flag to `false`.