# Ring Detection Approximation Mode

`detection.graph_engine.find_wash_rings` can switch to a sampled
approximation on very large wallet graphs so detection stays responsive
under heavy load.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| `approximation_threshold` | `LEDGERLENS_RING_APPROX_THRESHOLD` | unset (always exact) |
| `sample_node_budget` | `LEDGERLENS_RING_APPROX_SAMPLE_NODES` | same as the threshold |
| `seed` | — | `None` (non-deterministic) |

Arguments passed to the function take precedence over the environment
variables. When the graph has **more nodes than the threshold**, approximation
mode turns on automatically.

## Strategy

The sampler runs bounded random walks (32 steps each) along outgoing edges.
Each walk starts at a node chosen in proportion to its out-degree. It stops
once `sample_node_budget` distinct nodes have been collected. SCC-based ring
detection then runs on the induced subgraph. Because walks follow directed
edges, they tend to capture closed cycles (wash rings) whole. A ring can be
missed, but the sampler never invents one. Precision is therefore kept, and
only recall is lost.

## Flagging

In this mode, every ring carries `"approximate": True`. For exact results the
value is `False`. `build_ring_membership_index` copies the flag into
per-account metadata. A warning is also logged each time approximation mode
turns on.

## Accuracy trade-off

Run `python3 benchmarks/benchmark_ring_approximation.py [--budget N]`. It
builds a seeded, labeled graph with 200 planted 4-wallet rings (800
positive wallets) inside 50,000 background wallets that form no cycles.
It then reports wallet-level precision and recall for both modes. The
results below use seed 42:

| Sample budget | Precision (Δ) | Recall (Δ) |
|---|---|---|
| 5,000 | 1.000 (+0.000) | 0.165 (−0.835) |
| 10,000 | 1.000 (+0.000) | 0.310 (−0.690) |
| 25,000 | 1.000 (+0.000) | 0.785 (−0.215) |

Recall grows roughly with the fraction of the graph that is sampled. On
sparse graphs like this one, exact SCC search is already fast. The mode pays
off on very large, dense graphs, where the cost of cycle scoring in the exact
path dominates. Set the threshold so that approximation only turns on in
those overload conditions. Consumers that need full recall should either
ignore rings flagged `approximate` or re-run them in exact mode.
