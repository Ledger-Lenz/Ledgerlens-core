---
title: "Optimise Tarjan SCC Ring Detector for Graphs with 100K+ Nodes"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Profile and optimise `detection/graph_engine.py` for large-scale directed trade graphs. Replace the recursive Tarjan DFS with an iterative stack-based implementation to eliminate Python's recursion limit (`RecursionError` at ~1,000 nodes). Add a memory-mapped adjacency representation for graphs exceeding 50,000 nodes using `numpy` structured arrays or `scipy.sparse` CSR matrices. Target: process a 100,000-node, 500,000-edge trade graph in under 30 seconds on a single CPU core.

## Background & Context

`detection/graph_engine.py` currently implements Tarjan's SCC algorithm recursively. Python's default recursion limit is 1,000 frames (`sys.getrecursionlimit()`). Any trade graph with more than ~1,000 connected nodes causes a `RecursionError` during DFS — a hard crash. The Stellar DEX processes millions of transactions; production trade graphs routinely contain tens of thousands of distinct accounts.

The existing implementation also stores the adjacency list as a Python `dict[str, list[str]]`, which has poor cache locality and high per-object overhead. For a 100K-node graph, this representation consumes ~200MB of RAM and results in slow DFS due to Python object pointer chasing.

Two distinct optimisations are needed:

1. **Iterative Tarjan**: replace the recursive DFS with an explicit stack-based simulation. This eliminates the recursion limit entirely and is also slightly faster due to reduced Python frame overhead.

2. **Memory-mapped adjacency**: for graphs exceeding `GRAPH_MMAP_THRESHOLD` (default: 50,000 nodes), represent the adjacency as a `scipy.sparse.csr_matrix` (Compressed Sparse Row), which stores edges contiguously in memory and enables vectorised degree computations. Map node identifiers (Stellar G-addresses) to integer indices via a bijective dict.

The 30-second target for 100K nodes is achievable: iterative Tarjan on a 100K-node sparse graph has O(V+E) complexity; with CSR adjacency and a Python implementation, ~10M edge traversals per second is realistic.

## Objectives

- [ ] Implement `IterativeTarjanSCC` class in `detection/graph_engine.py` replacing the recursive implementation. Must produce identical SCC output for all test graphs.
- [ ] Replace recursive DFS with explicit `list`-based stack simulation of Tarjan's algorithm (including the `lowlink`, `on_stack`, and `index` arrays).
- [ ] Add `NodeIndex` class that provides O(1) `str→int` and `int→str` mapping for node identifiers.
- [ ] Implement `SparseTradeGraph` class using `scipy.sparse.csr_matrix` for graphs with `n_nodes >= GRAPH_MMAP_THRESHOLD`.
- [ ] `SparseTradeGraph.build_from_trades(trades)` constructs the CSR matrix from a list of `Trade` records.
- [ ] Maintain the existing public API: `TradeGraph.add_trade(trade)`, `TradeGraph.find_wash_rings()`, `TradeGraph.get_ring_members(wallet)` — callers must not need to know about the internal representation switch.
- [ ] Add `GRAPH_MMAP_THRESHOLD` configuration variable (default: 50,000).
- [ ] Profile the implementation with a synthetic 100K-node graph and record results in `docs/performance.md`.
- [ ] Ensure the output of `find_wash_rings()` is identical between the new and old implementations on all existing test fixtures.
- [ ] All new code covered by tests; ≥90% branch coverage.

## Technical Requirements

### Iterative Tarjan SCC algorithm

The iterative simulation mirrors the recursive algorithm but uses an explicit work stack. Each stack frame is represented as a tuple `(node, edge_iterator_position)`:

```python
def iterative_tarjan_scc(graph: dict[int, list[int]]) -> list[list[int]]:
    """
    Iterative Tarjan SCC. Returns list of SCCs (each SCC is a list of node indices).
    graph: adjacency dict {node_id: [neighbour_id, ...]}
    """
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = set()
    sccs = []

    def strongconnect(v):
        # Non-recursive simulation using an explicit call stack
        work_stack = [(v, iter(graph.get(v, [])))]
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        while work_stack:
            v, neighbours = work_stack[-1]
            try:
                w = next(neighbours)
                if w not in index:
                    # Tree edge: push w and continue from w
                    index[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work_stack.append((w, iter(graph.get(w, []))))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])
            except StopIteration:
                # All neighbours of v processed; pop v
                work_stack.pop()
                if work_stack:
                    parent = work_stack[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
                # SCC root check
                if lowlink[v] == index[v]:
                    scc = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        scc.append(w)
                        if w == v:
                            break
                    sccs.append(scc)

    for v in list(graph.keys()):
        if v not in index:
            strongconnect(v)

    return sccs
```

### `NodeIndex` class

```python
class NodeIndex:
    def __init__(self):
        self._str_to_int: dict[str, int] = {}
        self._int_to_str: list[str] = []

    def add(self, node: str) -> int:
        if node not in self._str_to_int:
            idx = len(self._int_to_str)
            self._str_to_int[node] = idx
            self._int_to_str.append(node)
        return self._str_to_int[node]

    def get_id(self, node: str) -> Optional[int]:
        return self._str_to_int.get(node)

    def get_node(self, idx: int) -> str:
        return self._int_to_str[idx]

    def __len__(self) -> int:
        return len(self._int_to_str)
```

### `SparseTradeGraph` class

```python
from scipy.sparse import csr_matrix, lil_matrix
import numpy as np

class SparseTradeGraph:
    def __init__(self, node_index: NodeIndex):
        self._node_index = node_index
        self._adj: Optional[csr_matrix] = None
        self._lil: lil_matrix = lil_matrix((0, 0))
        self._edge_data: dict[tuple[int, int], dict] = {}  # (i, j) -> {volume, count, timestamps}

    def build_from_trades(self, trades: list[Trade]) -> None:
        n = len(self._node_index)
        self._lil = lil_matrix((n, n), dtype=np.float64)
        for trade in trades:
            i = self._node_index.add(trade.base_account)
            j = self._node_index.add(trade.counter_account)
            self._lil[i, j] += float(trade.base_amount)
        self._adj = csr_matrix(self._lil)

    def to_adjacency_dict(self) -> dict[int, list[int]]:
        """Convert CSR to adjacency dict for Tarjan SCC."""
        if self._adj is None:
            return {}
        adj = {}
        cx = self._adj.tocsr()
        for i in range(cx.shape[0]):
            start, end = cx.indptr[i], cx.indptr[i+1]
            if end > start:
                adj[i] = cx.indices[start:end].tolist()
        return adj
```

### Performance target and profiling

Run `python -m cProfile -o profile.stats` on a synthetic graph with 100K nodes and 500K edges (use `ingestion/synthetic_data.py` to generate). Profile and add results to `docs/performance.md`:

| Implementation | Nodes | Edges | Time (s) | Peak RAM (MB) |
|---|---|---|---|---|
| Recursive (baseline) | 10K | 50K | ? | ? |
| Iterative | 10K | 50K | ? | ? |
| Iterative + CSR | 100K | 500K | <30 | <500 |

### Backward compatibility

The public API must remain unchanged:
```python
# Callers use this API unchanged:
graph = TradeGraph()
for trade in trades:
    graph.add_trade(trade)
rings = graph.find_wash_rings()
members = graph.get_ring_members(wallet)
```

`TradeGraph.__init__` selects `SparseTradeGraph` vs dict-based adjacency based on node count after `add_trade` calls.

## Security Considerations

- The iterative Tarjan must handle graphs with self-loops (a wallet trading with itself) without infinite loops. Add a guard: skip self-loop edges (`if w == v: continue`) in the adjacency traversal.
- The CSR matrix allocation (`lil_matrix((n, n))`) could exhaust memory for adversarially large `n` (e.g., n=10M). Add a hard cap: `MAX_GRAPH_NODES = 1_000_000`; raise `GraphTooLargeError` if exceeded. Log the node count at INFO level before building the CSR matrix.
- Synthetic graph generation for benchmarking must not be committed to the repository (only used in CI performance tests). Generated graphs should be created in `tmp/` and cleaned up after tests.

## Testing Requirements

- **Correctness — SCC output equivalence**: for every existing test fixture in `tests/test_graph_engine.py`, assert that `IterativeTarjanSCC` produces the same SCCs as the reference recursive implementation (sorted for comparison).
- **Correctness — no recursion limit**: generate a linear chain of 2,000 nodes (would cause `RecursionError` in recursive Tarjan); assert iterative implementation completes without error.
- **Correctness — self-loop handling**: graph with a self-loop; assert no infinite loop; assert SCC contains only the self-loop node.
- **Correctness — disconnected graph**: assert all nodes appear in exactly one SCC.
- **Unit — `NodeIndex` bijection**: add 1,000 nodes; assert `get_node(get_id(node)) == node` for all nodes.
- **Unit — `SparseTradeGraph.to_adjacency_dict`**: build from 5 trades; assert dict matches expected adjacency.
- **Unit — `GraphTooLargeError`**: attempt to build graph with `MAX_GRAPH_NODES + 1` nodes; assert error raised.
- **Performance — 100K nodes in <30s**: use `pytest-benchmark` or `timeit`; fail if median time exceeds 30s.
- **Performance — peak RAM <500MB** for 100K-node CSR graph: use `tracemalloc`.

## Documentation Requirements

- Docstrings on `IterativeTarjanSCC`, `SparseTradeGraph`, and `NodeIndex`.
- Update `README.md` Graph-Based Ring Detection section to note iterative implementation and scale targets.
- New section in `docs/performance.md` with profiling results table.
- Document `GRAPH_MMAP_THRESHOLD` and `MAX_GRAPH_NODES` in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `IterativeTarjanSCC` implemented; produces identical output to recursive implementation on all existing test fixtures.
- [ ] `RecursionError` no longer occurs for any graph size below `MAX_GRAPH_NODES`.
- [ ] `SparseTradeGraph` (CSR-based) used for graphs with `n >= GRAPH_MMAP_THRESHOLD`.
- [ ] Public `TradeGraph` API unchanged; callers require no code changes.
- [ ] Performance test: 100K nodes, 500K edges in <30s.
- [ ] Memory usage: 100K-node CSR graph uses <500MB peak RAM.
- [ ] `GraphTooLargeError` raised for graphs exceeding `MAX_GRAPH_NODES`.
- [ ] All correctness and performance tests pass; ≥90% branch coverage.
- [ ] `docs/performance.md` updated with profiling results.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have deep familiarity with graph algorithms — specifically Tarjan's SCC and DFS-based algorithms — and their iterative simulation via explicit stack. You understand the memory layout of `scipy.sparse` CSR matrices and why they outperform Python dicts for large sparse graphs. Experience profiling Python code with `cProfile` or `py-spy` and optimising for both time and memory is essential. Prior work on large-scale graph processing (NetworkX, iGraph, or custom implementations) will translate directly.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., graph algorithms, Python performance optimisation, large-scale data structures).
2. **Relevant experience**: iterative SCC implementations, large-scale graph processing, or Python performance optimisation work you have done.
3. **Approach / thoughts**: would you use `scipy.sparse.csr_matrix` or a `numpy`-backed adjacency array? What is your strategy for handling dynamic graph updates (new trades arriving) without rebuilding the full CSR matrix each time?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
