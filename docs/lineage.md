# OpenLineage Lineage Tracking in LedgerLens

LedgerLens implements OpenLineage-compatible event emission to capture operational metadata about pipelines. This allows engineers to trace which ingestion batch produced a given model or feature distribution snapshot, resolving tracing issues when feature drift occurs.

## Concepts
OpenLineage defines a standard schema for tracking lineage as a DAG of **Jobs**, **Runs**, and **Datasets**:
- **Job**: A process that consumes inputs and produces outputs (e.g., `feature_engineering.build_feature_vector`).
- **Run**: A specific execution of a Job (e.g., a single pipeline pass tagged with a correlation ID).
- **Dataset**: An input or output data source (e.g., a SQLite database table or versioned model joblib file).

## Instrumented Stages
1. **Ingestion**:
   - `ingestion.historical_loader.fetch_chunk` (Parallel historical trade ingestion chunks)
   - `ingestion.horizon_streamer.stream` / `ingestion.horizon_streamer.run` (SSE streaming connections)
2. **Feature Engineering**:
   - `feature_engineering.build_feature_vector` (Batch or streaming scoring passes)
3. **Model Training**:
   - `model_training.train_ensemble` (Training/retraining cycles)

## Running Marquez Locally
You can visualize the lineage graph using [Marquez](https://marquezproject.github.io/marquez/), a vendor-neutral metadata service that naturally consumes OpenLineage events.

To run a local Marquez instance via Docker:
```bash
docker run -d -p 5000:5000 -p 5002:5002 --name marquez marquezproject/marquez:latest
```

Once Marquez is running, configure LedgerLens in your `.env` file to send HTTP events:
```env
LINEAGE_ENABLED=true
LINEAGE_BACKEND=http
OPENLINEAGE_URL=http://localhost:5000
```
Open your browser to `http://localhost:3000` to view the Marquez UI and explore the lineage graph.

## Lineage REST API
LedgerLens includes a built-in admin-only REST API endpoint to retrieve the lineage graph locally without any external dependencies:

```bash
GET /admin/lineage/{dataset}
```

### Example Graph Query
To fetch the lineage graph for the `trades` dataset:
```bash
curl -H "X-LedgerLens-Admin-Key: <your_admin_key>" http://localhost:8000/admin/lineage/trades
```

This returns a JSON representation of the DAG:
```json
{
  "nodes": [
    {
      "id": "dataset:horizon:trades",
      "type": "dataset",
      "name": "trades",
      "namespace": "horizon"
    },
    {
      "id": "job:ledgerlens-core:ingestion.historical_loader.fetch_chunk",
      "type": "job",
      "name": "ingestion.historical_loader.fetch_chunk",
      "namespace": "ledgerlens-core"
    },
    {
      "id": "dataset:ledgerlens-core.sqlite:trades",
      "type": "dataset",
      "name": "trades",
      "namespace": "ledgerlens-core.sqlite"
    }
  ],
  "edges": [
    {
      "source": "dataset:horizon:trades",
      "target": "job:ledgerlens-core:ingestion.historical_loader.fetch_chunk"
    },
    {
      "source": "job:ledgerlens-core:ingestion.historical_loader.fetch_chunk",
      "target": "dataset:ledgerlens-core.sqlite:trades"
    }
  ]
}
```
