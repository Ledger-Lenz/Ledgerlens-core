# ingestion/

The `ingestion/` package is responsible for pulling trade and event data into LedgerLens from every supported source — the Stellar Horizon API, EVM chains (Ethereum, Base, Polygon), Solana, and cross-chain bridges — and preparing it for the detection engine. It also contains the transformation utilities, checkpointing infrastructure, deduplication, and observability helpers that keep the pipeline reliable at production scale.

For a full conceptual overview see [../docs/ingestion.md](../docs/ingestion.md).

---

## File index

### Stellar / Horizon loaders

| File | Description |
|---|---|
| `horizon_streamer.py` | Real-time SSE trade stream from the Horizon API with checkpoint persistence |
| `historical_loader.py` | Parallel bulk historical trade backfill (chunked, resumable) |
| `operations_loader.py` | Order-book event ingestion (offer create/update/cancel) from Horizon operations |
| `account_loader.py` | Account funding-source and creation-time metadata for wallet-graph features |
| `path_payment_loader.py` | Path-payment operation ingestion for multi-hop trade detection |

### EVM and cross-chain loaders

| File | Description |
|---|---|
| `evm_loader.py` | Multi-provider EVM trade and bridge-event loader (Ethereum, Base, Polygon) with circuit breaker and health scoring |
| `bridge_loader.py` | Allbridge bridge event ingestion for cross-chain wallet linking |
| `solana_adapter.py` | Solana DEX trade ingestion adapter |
| `amm_loader.py` | On-chain AMM (automated market maker) liquidity event ingestion |
| `uniswap_adapter.py` | Uniswap-specific adapter (V2/V3 swap events) |
| `curve_adapter.py` | Curve Finance pool swap event adapter |

### Transformation and pipeline utilities

| File | Description |
|---|---|
| `filters.py` | Configurable trade filter pipeline (asset pair whitelist/blacklist, min volume, asset type, account exclusion) with hot-reload and SQLite rejection storage |
| `dedup.py` | Trade deduplication layer (hash-based, bloom-filter backed) |
| `data_models.py` | Pydantic schemas for `Trade`, `Asset`, and `OrderBookEvent` records |
| `synthetic_data.py` | Synthetic trade and wash-ring generator for local training and testing |
| `adversarial_data.py` | Adversarial trade data generator for robustness testing |
| `graph_builder.py` | Incremental trade graph construction for ingestion-time features |

### Checkpointing and reliability

| File | Description |
|---|---|
| `checkpoint.py` | Durable checkpoint store for historical backfill resume |
| `stream_checkpoint.py` | Horizon stream cursor checkpoint (SSE position persistence) |
| `replay_buffer.py` | In-memory replay buffer for stream backpressure handling |
| `dlq.py` | Dead-letter queue for failed trade records |

### HTTP and observability

| File | Description |
|---|---|
| `http_client.py` | Retrying HTTP helper for Horizon and EVM API calls (exponential backoff, circuit breaker) |
| `metrics.py` | Prometheus ingestion metrics (throughput, latency, error rates) |
| `rate_limiter.py` | Per-provider rate limiting for outbound API calls |
| `parquet_exporter.py` | Parquet export of ingested trade data for archival and cold-tier storage |

---

## Related documentation

- [../docs/ingestion.md](../docs/ingestion.md) — Architecture overview and data flow
- [../docs/filter-pipeline.md](../docs/filter-pipeline.md) — Filter pipeline configuration reference
- [../docs/cross_chain_detection.md](../docs/cross_chain_detection.md) — Cross-chain wallet linking and round-trip detection
- [../docs/streaming_api.md](../docs/streaming_api.md) — Horizon SSE streaming design
- [../docs/streaming_scorer.md](../docs/streaming_scorer.md) — Incremental scoring on the live stream
- [../docs/performance.md](../docs/performance.md) — Scale targets and sharded graph engine
- [../docs/api/ingestion.md](../docs/api/ingestion.md) — Ingestion API reference
