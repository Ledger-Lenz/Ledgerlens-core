# Vector Similarity Search for Structurally Similar Wallets

## Overview

This feature adds a persistent embedding store and approximate nearest neighbor (ANN) index to find structurally similar wallets across the entire trade graph, not just within a queried wallet's local ego-network.

## Motivation

The existing `top_neighbours` field in `/gnn/ring-score/{wallet}` only searches within a small subgraph (last 500 trades involving the wallet). This means:
- Wallets that are structurally identical but have no direct trade relationship are never found.
- Embeddings are recomputed from scratch on every request, which is inefficient.

## Architecture

### Components

| Component | Description |
|-----------|-------------|
| `detection/embedding_store.py` | SQLite-based persistence for wallet embeddings (stores wallet address, model version, embedding vector, timestamp). |
| `detection/vector_index.py` | FAISS-based ANN index (supports flat and IVF backends for scalability). |
| `cli.py compute-embeddings` | Batch job to compute and store embeddings for all wallets in the recent activity window. |
| `GET /v1/wallets/{wallet}/similar` | API endpoint to query the global index for similar wallets. |

### Index Backends

| Backend | Use Case |
|---------|----------|
| `faiss_flat` | Exact cosine similarity search, good for small datasets (< 50k wallets). |
| `faiss_ivf` | Approximate nearest neighbor search using inverted file index, better for large datasets (> 50k wallets). |

## Usage

### Compute Embeddings

Run the batch job to compute and store embeddings for all wallets in the last N days:

```bash
python cli.py compute-embeddings --window-days 30
```

### Query Similar Wallets

```bash
curl -X GET "http://localhost:8000/v1/wallets/GABC123/similar?k=5"
```

Response:

```json
{
  "wallet": "GABC123",
  "computed_from": "global_index",
  "model_version": "gnn_v1_20250101",
  "similar_wallets": [
    {
      "wallet": "GDEF456",
      "similarity": 0.94,
      "current_risk_score": 85,
      "wash_ring_membership": true
    },
    {
      "wallet": "GHIJ789",
      "similarity": 0.89,
      "current_risk_score": 12,
      "wash_ring_membership": false
    }
  ]
}
```

## Configuration

Add to your `.env`:

```env
VECTOR_INDEX_BACKEND=faiss_flat
VECTOR_INDEX_DIM=64
VECTOR_INDEX_IVF_THRESHOLD=50000
VECTOR_INDEX_REFRESH_SECONDS=300
EMBEDDING_STORE_PATH=./data/wallet_embeddings.db
GNN_SIMILARITY_RATE_LIMIT_PER_MINUTE=10
```

| Variable | Description |
|----------|-------------|
| `VECTOR_INDEX_BACKEND` | Index backend: `faiss_flat` (default) or `faiss_ivf`. |
| `VECTOR_INDEX_DIM` | Embedding dimension (default 64). |
| `VECTOR_INDEX_IVF_THRESHOLD` | Number of wallets above which to switch to IVF index (default 50k). |
| `VECTOR_INDEX_REFRESH_SECONDS` | How often to refresh the index in memory (default 300 seconds). |
| `EMBEDDING_STORE_PATH` | Path to SQLite database for embeddings. |
| `GNN_SIMILARITY_RATE_LIMIT_PER_MINUTE` | Rate limit for similarity queries (lower than general API limit for security). |

## Security Considerations

1. **No raw embeddings exposed**: The API only returns wallet addresses and similarity scores, never raw embedding vectors.
2. **Rate limiting**: Similarity queries are rate-limited more aggressively than other endpoints to prevent scraping the entire wallet population via similarity.
3. **Audit logging**: All similarity queries are logged in the audit log for traceability.

## Integration with Existing Features

The new `/v1/wallets/{wallet}/similar` endpoint complements the existing `top_neighbours` field in `/gnn/ring-score/{wallet}`:
- Use `top_neighbours` for a quick check of nearby, recently trading wallets.
- Use `/v1/wallets/{wallet}/similar` to find structurally similar wallets across the entire graph, including those with no direct trade relationship.

See [docs/gnn_ring_detection.md](./gnn_ring_detection.md) for more details on the difference.

## Testing

Run the test suite for the new components:

```bash
pytest tests/test_embedding_store.py tests/test_vector_index.py
```
