# Vector Index Capacity Limits

Capacity-planning figures for `detection/vector_index.py` (`FaissVectorIndex`)
at up to 10x the current production scale, and beyond.

## Method

Reproduce with:

```bash
python3 benchmarks/benchmark_vector_index.py                     # 50k → 500k (10x)
python3 benchmarks/benchmark_vector_index.py --scales 1000000 2000000 --queries 200
```

- **Current scale** is taken as 50 000 wallets — the default
  `vector_index_ivf_threshold`, i.e. the size the index is configured for today.
  10x is therefore 500 000 wallets.
- Synthetic, seeded, clustered 64-dim embeddings (`vector_index_dim` default),
  ~1 cluster per 500 wallets, so IVF sees non-uniform data like real embeddings.
- Index configuration is the shipped one: `faiss_flat` = `IndexFlatIP`;
  `faiss_ivf` = `IndexIVFFlat` with `nlist=100`, `nprobe=10`.
- **Build** = `add_batch` of all vectors (incl. IVF training). **Memory** =
  serialized FAISS index + wallet-id bookkeeping. **Latency** = single-query
  `search(k=10)`. **Recall@10** = overlap of IVF results with exact flat results.
- Hardware: 2 vCPU / 8 GB RAM Linux container, faiss-cpu 1.15.1, Python 3.14.

## Results

| Wallets | Scale | Backend | Build (s) | Memory (MB) | p50 (ms) | p99 (ms) | Recall@10 |
|--------:|------:|---------|----------:|------------:|---------:|---------:|----------:|
| 50,000 | 1x | faiss_flat | 0.02 | 19.6 | 1.16 | 3.31 | 1.000 |
| 50,000 | 1x | faiss_ivf | 0.25 | 20.0 | 0.12 | 1.15 | 1.000 |
| 100,000 | 2x | faiss_flat | 0.05 | 39.1 | 2.31 | 6.25 | 1.000 |
| 100,000 | 2x | faiss_ivf | 0.32 | 40.0 | 0.14 | 0.46 | 1.000 |
| 250,000 | 5x | faiss_flat | 0.15 | 95.9 | 6.75 | 15.17 | 1.000 |
| 250,000 | 5x | faiss_ivf | 0.58 | 98.0 | 0.61 | 2.49 | 1.000 |
| **500,000** | **10x** | faiss_flat | 0.32 | 191.9 | 13.87 | 30.91 | 1.000 |
| **500,000** | **10x** | faiss_ivf | 0.99 | 195.9 | 1.00 | 1.46 | 1.000 |
| 1,000,000 | 20x | faiss_flat | 0.58 | 383.8 | 29.69 | 56.21 | 1.000 |
| 1,000,000 | 20x | faiss_ivf | 1.71 | 391.8 | 2.08 | 3.19 | 1.000 |
| 2,000,000 | 40x | faiss_flat | 1.36 | 767.5 | 58.79 | 100.10 | 1.000 |
| 2,000,000 | 40x | faiss_ivf | 3.08 | 783.5 | 4.47 | 7.07 | 1.000 |

Memory is ~390 bytes/wallet for both backends (256 B of float32 vector plus
wallet-id bookkeeping) and grows linearly; it is not the binding constraint
below ~10M wallets on an 8 GB node. Build time stays in seconds throughout.

## Capacity limit and degradation point

The acceptance budget is derived from the scoring target of **p99 < 50 ms per
wallet** (`benchmarks/benchmark_scoring.py`): a similarity lookup should use no
more than 20% of it, i.e. **search p99 ≤ 10 ms**.

| Backend | Degrades beyond | Evidence |
|---------|-----------------|----------|
| `faiss_flat` | **~150 000 wallets (3x)** | p99 crosses 10 ms between 100k (6.3 ms) and 250k (15.2 ms); at 10x it is 31 ms, and at 1M it breaches the whole 50 ms scoring budget. Latency is linear in wallet count (brute-force scan). |
| `faiss_ivf` (`nlist=100`, `nprobe=10`) | **~2 500 000 wallets (50x)** | p99 7.1 ms at 2M. With a fixed `nlist=100` each list holds `N/100` vectors and every query scans 10% of the index, so latency is still linear in N — just 10x lower than flat. |

At the 10x target (500 000 wallets) **only `faiss_ivf` is within budget**.

### Known limitation: flat → IVF auto-switch loses data

`FaissVectorIndex.add_batch` switches a `faiss_flat` index to IVF once
`vector_index_ivf_threshold` is crossed, recovering existing vectors via
`_get_all_vectors()`. That helper reads `IndexFlat.xb`, which current FAISS
releases do not expose, so it returns an empty array: after the switch only the
newly added batch is indexed while `size()` still counts every wallet
(observed: 6 000 wallets tracked, 2 000 in `ntotal`). Until that is fixed,
deployments expecting to grow past the threshold should **configure
`vector_index_backend=faiss_ivf` explicitly** and rebuild from the embedding
store rather than rely on the auto-switch.

## Scaling recommendations

1. **Up to ~150k wallets:** `faiss_flat` is fine and exact.
2. **150k – ~2.5M wallets:** use `faiss_ivf` explicitly (see limitation above).
   Scale `nlist` with the index instead of the fixed 100 — FAISS guidance is
   `nlist ≈ 4·√N` (≈ 2 800 at 500k, 4 000 at 1M) — and retune `nprobe` so
   recall@10 stays ≥ 0.95; this keeps per-query scan size roughly constant and
   moves the IVF limit well past 10M. Require ≥ 39·`nlist` training vectors.
3. **Beyond ~2.5M wallets or ~10M with tuned IVF:** shard the index by wallet
   hash (as `detection/graph_sharding.py` does for the graph), query shards in
   parallel and merge top-k; or switch to a compressed index
   (`IndexIVFPQ`, e.g. `m=16` → 16 B/vector, ~16x less memory, at some recall
   cost) or graph-based `IndexHNSWFlat` for sub-millisecond latency at higher
   memory.
4. Re-run `benchmarks/benchmark_vector_index.py` after changing `dim`,
   `nlist`/`nprobe`, or hardware, and update this table.
