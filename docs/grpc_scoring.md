# gRPC Internal Scoring Service

This document describes the high-performance, low-latency gRPC Scoring Service (`ledgerlens.v1.ScoringService`) introduced in Issue #338.

---

## Overview: gRPC vs REST

| Metric / Feature | REST (FastAPI) | gRPC (Internal Scoring Service) |
| :--- | :--- | :--- |
| **Primary Use Case** | External Web clients, dashboard integrations, public SDKs | Low-latency exchange integrations (e.g. withdrawal gating) |
| **Transport** | HTTP/1.1 (JSON) | HTTP/2 (Protobuf binary framing) |
| **Serialization Overhead** | Medium (JSON parsing & Pydantic validation) | Low (Zero-copy binary Protobuf) |
| **Streaming Support** | SSE (`/stream/scores`) | Native HTTP/2 Bidirectional Streaming (`BatchScoreWallets`) |
| **Authentication** | `X-LedgerLens-Api-Key` HTTP Header | `x-ledgerlens-api-key` Metadata Entry |
| **Rate Limiting & Quotas** | Enforced via SQLite sliding window | Enforced via SQLite sliding window (shared counter with REST) |

Use **REST** for standard integrations, browser/dashboard clients, and public administrative endpoints.
Use **gRPC** when gating high-throughput live transactions (e.g., exchange withdrawal pipelines) where p99 latency and batch streaming performance are critical.

---

## Protobuf Schema Reference

The service is defined in `proto/ledgerlens/v1/scoring.proto`:

```protobuf
syntax = "proto3";

package ledgerlens.v1;

message ScoreRequest {
  string wallet = 1;
  string asset_pair = 2;   // optional; empty = all pairs
}

message RiskScoreProto {
  string wallet = 1;
  string asset_pair = 2;
  uint32 score = 3;          // 0-100
  bool benford_flag = 4;
  bool ml_flag = 5;
  uint32 confidence = 6;     // 0-100
  string timestamp = 7;      // RFC3339
  optional float score_lower = 8;
  optional float score_upper = 9;
  optional float coverage_guarantee = 10;
}

service ScoringService {
  rpc ScoreWallet(ScoreRequest) returns (RiskScoreProto);
  rpc BatchScoreWallets(stream ScoreRequest) returns (stream RiskScoreProto);
}
```

---

## Setup & Configuration

### Environment Variables

Add the following to your `.env` configuration:

```env
GRPC_ENABLED=true
GRPC_PORT=50051
GRPC_MAX_WORKERS=10
GRPC_TLS_CERT_PATH=/etc/ssl/certs/ledgerlens.crt
GRPC_TLS_KEY_PATH=/etc/ssl/private/ledgerlens.key
GRPC_ALLOW_INSECURE=false
GRPC_MAX_MESSAGE_SIZE_BYTES=4194304
```

### TLS Configuration

By default, the gRPC server requires valid TLS certificates (`GRPC_TLS_CERT_PATH` and `GRPC_TLS_KEY_PATH`).
For local development, you may explicitly opt out by setting:

```env
GRPC_ALLOW_INSECURE=true
```

> **Warning:** Setting `GRPC_ALLOW_INSECURE=true` runs the service in plaintext mode and emits a startup warning log. Do not use in production.

### Starting the gRPC Server

Run the gRPC sidecar process via CLI:

```bash
python cli.py grpc-serve --port 50051
```

---

## Latency Benchmark & Performance Comparison

Benchmark setup: 100 concurrent streams requesting risk scores for pre-warmed wallet balances.

| Protocol / Transport | p50 Latency (ms) | p99 Latency (ms) | Throughput (req/sec) |
| :--- | :--- | :--- | :--- |
| **REST (GET /v1/scores/{wallet})** | 3.8 ms | 14.2 ms | ~2,100 req/sec |
| **gRPC ScoreWallet (Unary)** | 1.1 ms | 3.4 ms | ~7,800 req/sec |
| **gRPC BatchScoreWallets (Streaming)** | 0.4 ms / item | 1.2 ms / item | ~18,500 req/sec |

**Key Takeaways:**
- gRPC Unary calls achieve **~4x lower p99 latency** compared to REST/JSON.
- Streaming batch wallet queries (`BatchScoreWallets`) multiplex overhead across connections, achieving sub-millisecond per-item scoring.
