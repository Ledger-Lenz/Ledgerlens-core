# Event Bus Integration

The Ledgerlens Core application can publish `RiskScore` records to an external Event Bus (Kafka or NATS JetStream). This is the recommended pattern to reliably hand off scores from the core pipeline to downstream consumers like `ledgerlens-api`.

## Architecture

The event bus integration acts as an additive outbound synchronization mechanism. The local SQLite database (`ledgerlens.db`) remains the durable system of record. The moment a `RiskScore` is computed and committed to SQLite, it is immediately published to the event bus.

If the event bus is down, the core pipeline logs the failure, increments a metric, and continues processing. No pipeline runs are failed due to broker unavailability.

## Configuration

Configure the event bus in your `.env` file. By default, it is disabled (`none`).

### Kafka
```env
EVENT_BUS_BACKEND=kafka
EVENT_BUS_KAFKA_BOOTSTRAP_SERVERS=localhost:9092
EVENT_BUS_KAFKA_TOPIC=ledgerlens.riskscore.v1
EVENT_BUS_KAFKA_SASL_PASSWORD=your_secret
```

### NATS JetStream
```env
EVENT_BUS_BACKEND=nats
EVENT_BUS_NATS_SERVERS=nats://localhost:4222
EVENT_BUS_NATS_SUBJECT=ledgerlens.riskscore.v1
EVENT_BUS_NATS_TOKEN=your_token
```

## Consumer Contract

Downstream consumers (`ledgerlens-api`, analytics sinks, etc.) must adhere to the following contract:

- **Partition Key**: `f"{wallet}:{asset_pair}"`. Events for the same wallet and asset pair are strictly ordered within the same partition.
- **Idempotency**: The bus provides **at-least-once** delivery. Consumers **MUST** treat `(wallet, asset_pair, timestamp)` as an idempotency key. A newer `timestamp` overwrites an older one. If an older `timestamp` arrives after a newer one, the consumer should discard it.
- **Schema Versioning**: The payload structure is wrapped in an envelope. Evolution of the `data` schema (such as adding conformal prediction fields) is backward compatible. Breaking changes will increment the `schema_version`.

### Event Envelope Example
```json
{
  "schema_version": 1,
  "event": "risk_score.updated",
  "produced_at": "2026-07-17T12:00:00Z",
  "producer": "ledgerlens-core",
  "data": {
    "wallet": "GABCD...WXYZ",
    "asset_pair": "XLM/USDC",
    "score": 85,
    "benford_flag": true,
    "ml_flag": true,
    "confidence": 90,
    "disputed": false,
    "timestamp": "2026-07-17T12:00:00Z",
    "score_lower": 78.2,
    "score_upper": 91.4,
    "prediction_set": [1],
    "coverage_guarantee": 0.9
  }
}
```

## Bootstrapping / Backlog Replay

If you spin up a new consumer or recover from an extended event bus outage, you can replay past risk scores from the SQLite database onto the event bus:

```bash
python cli.py publish-backlog --since 2026-07-01T00:00:00Z
```
This replays all scores generated at or after the provided timestamp in chronological order.
