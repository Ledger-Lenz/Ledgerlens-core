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
| Iterative + CSR        | 100 K  | 500 K   | 4.5        | 23.0          | ~27.4     | 62.5          |

Target: 100 K nodes, 500 K edges in **< 30 s** on a single CPU core with **< 500 MB** peak RAM.

## Notes

### Iterative Tarjan (`IterativeTarjanSCC`)

Replaces networkx's recursive Tarjan with an explicit work-stack. This eliminates Python's default recursion limit of ~1 000 frames which previously caused `RecursionError` for graphs with more than ~1 000 nodes in a single strongly-connected component.

Time complexity: O(V + E). For a 100 K-node, 500 K-edge graph: approximately 1.2 s for the Tarjan traversal itself.

### CSR Adjacency (`SparseTradeGraph`)

For graphs with `n_nodes >= GRAPH_MMAP_THRESHOLD` (default 50 000), the adjacency list is stored as a `scipy.sparse.csr_matrix`. Building the CSR matrix from aggregated edge data takes approximately 3.5 s for 500 K edges; `to_adjacency_dict()` then takes ~0.1 s.

Memory for the CSR matrix at 500 K edges: ~4 MB data (float64) + ~1 MB indices (int32). The `lil_matrix` used during construction has higher transient memory but is freed immediately after `csr_matrix()` conversion.

### Ring metric computation

For the typical 100 K-node random graph, a single giant SCC containing ~98 % of all nodes is detected and flagged as truncated (exceeds `max_ring_size=10`). Metrics (total volume, timing tightness, avg trade count) are computed with a single O(E) pass over the aggregated edge-data dict, avoiding the overhead of building a full `networkx.DiGraph` for large SCCs. The remaining SCCs are singletons and are skipped by the `min_ring_size=3` filter.

### Threshold settings

| Variable              | Default  | Description                                                     |
| --------------------- | -------- | --------------------------------------------------------------- |
| `GRAPH_MMAP_THRESHOLD`| 50 000   | Node count above which CSR adjacency is used instead of a dict  |
| `MAX_GRAPH_NODES`     | 1 000 000| Hard cap; `GraphTooLargeError` is raised above this limit       |
