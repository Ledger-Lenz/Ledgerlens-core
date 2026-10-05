# Graph Retention and Eviction

`ingestion.graph_builder.TemporalGraphBuilder` keeps a wallet → node-index map
while building temporal snapshots. Without a bound, every wallet ever seen is
retained and each snapshot copies the full map, so memory grows without limit in
long-running ingestion.

## Policy

```python
TemporalGraphBuilder(bucket_hours=4, retention_buckets=42)  # 7-day window
```

- `retention_buckets=None` (default) keeps the legacy unbounded behaviour.
- With `retention_buckets=N`, after each bucket is ingested, any wallet with no
  trade in the last `N` buckets (including the current one) is evicted and the
  node indices are compacted.
- Eviction happens *after* the current bucket's wallets are marked active, so a
  wallet referenced by any edge in the snapshot being built is never evicted.
  A ring whose legs straddle the window boundary keeps every member that traded
  within the window.

## Metrics

`builder.stats` exposes memory-usage counters suitable for soak-test tracking:

| Key | Meaning |
| --- | --- |
| `tracked_wallets` | wallets currently indexed |
| `peak_tracked_wallets` | high-water mark since construction |
| `evicted_wallets` | cumulative wallets evicted |

With a fixed window, `peak_tracked_wallets` plateaus at roughly
`active wallets per bucket × retention_buckets` regardless of run length
(see `tests/test_graph_builder.py::test_retention_window_bounds_memory_in_soak`).

## Detection-accuracy trade-offs

- Rings/cycles whose legs are further apart than the window are no longer
  connected in a single node index and may be missed by `detection/graph_engine.py`.
  Choose `retention_buckets * bucket_hours` at least as large as the longest
  cycle duration you need to detect.
- Node indices are not stable across snapshots once eviction runs; consumers must
  key by wallet address (`snapshot.wallet_index`), not by raw index.
- A wallet that returns after eviction is treated as new and gets a new index.
