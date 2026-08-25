# Client-Binding Strategy (Issue #691)

## Decision

Use **protocol-first generation with thin hand-written wrappers** for typed
request/response protocols, and **hand-written resilience adapters** for
real-time protocols:

- **gRPC:** generate transport bindings from `proto/ledgerlens/v1/scoring.proto`;
  expose an idiomatic wrapper per SDK for authentication, deadlines, retries,
  and error mapping.
- **GraphQL:** generate schema and operation types from the GraphQL introspection
  result; keep queries and the public SDK facade hand-written so operation
  selection remains explicit.
- **WebSocket/SSE:** use the standard client primitive for each language and
  implement a small shared-behaviour adapter for reconnect, backoff,
  cancellation, event IDs, and bounded buffering. Code generation does not
  solve these transport-lifecycle concerns.

This avoids maintaining nine independent wire implementations while keeping
public SDK APIs idiomatic and preventing generated types from leaking into
application code.

## Why this is the recommended approach

| Protocol | Recommended binding | Why | Main risk |
|---|---|---|---|
| gRPC | Proto-first generated stubs + thin wrappers | The checked-in `.proto` is the canonical contract, supports unary and bidirectional streaming, and gives consistent wire compatibility across Go, Python, and TypeScript. | Generated-code/toolchain drift when the proto or compiler versions change. |
| GraphQL | Introspection/schema-generated types + hand-written operations | Generated types catch schema drift while explicit operations avoid shipping an unnecessarily broad client surface. | The runtime schema and generated snapshot can diverge unless CI checks freshness. |
| WebSocket/SSE | Hand-written lifecycle adapters over standard primitives | Reconnection, `Last-Event-ID`, heartbeats, cancellation, backpressure, and browser compatibility are behavioural concerns rather than schema concerns. | Subtle differences in buffering and reconnect policy across languages. |

## Priority order

1. **gRPC Python client POC** — validate the generated contract first with the
   repository's existing Python bindings and server tests. This is the lowest
   bootstrap-risk path and exercises both unary and streaming RPCs immediately.
2. **gRPC Go client** — productionize the lowest-latency path for exchange
   withdrawal-gating and other backend integrations. Generate from the same
   proto, then add a wrapper that maps gRPC status codes and metadata to the Go
   SDK's error model.
3. **GraphQL TypeScript client** — the dashboard is the most immediate consumer
   of typed, composed queries. Add an introspection/codegen freshness check and
   keep generated artifacts isolated from the public facade.
4. **WebSocket/SSE TypeScript client** — dashboards benefit most from live score
   updates. Prefer SSE for one-way score updates and WebSocket only where
   bidirectional control is required.
5. **Python and Go streaming adapters** — implement the same event and retry
   contract after the TypeScript behaviour is exercised in production-like
   conditions.

The first three steps establish the typed-contract pipeline; the last two
address real-time lifecycle complexity without duplicating protocol schemas.

## Proof of concept

`scripts/grpc_client_poc.py` is a small client against the real
`ledgerlens.v1.ScoringService` generated Python bindings. It exercises both the
unary `ScoreWallet` RPC and the streaming `BatchScoreWallets` RPC, sends the
same API-key metadata as the server expects, and supports TLS by default.

Start the local sidecar in one terminal:

```bash
LEDGERLENS_ADMIN_API_KEY=local-dev-key \
GRPC_ALLOW_INSECURE=true \
python cli.py grpc-serve --port 50051
```

Run the POC in another terminal:

```bash
LEDGERLENS_API_KEY=local-dev-key \
python scripts/grpc_client_poc.py \
  --endpoint localhost:50051 \
  --insecure \
  --wallet GABC...XYZ
```

For production-like TLS, omit `--insecure` and provide the CA certificate when
using a private certificate authority:

```bash
LEDGERLENS_API_KEY="$LEDGERLENS_API_KEY" \
python scripts/grpc_client_poc.py \
  --endpoint scoring.example.com:443 \
  --ca-cert /etc/ledgerlens/ca.pem \
  --wallet GABC...XYZ
```

The existing `tests/test_grpc_scoring_service.py` verifies the same generated
stub against the in-process LedgerLens server, including authentication,
optional conformal fields, batch ordering, batch limits, and rate limiting.
Together, the test and script validate the wire contract without introducing a
second hand-written protocol implementation.

## Drift controls

- Treat `proto/ledgerlens/v1/scoring.proto` as the source of truth.
- Pin protoc/plugin versions in the future codegen workflow and regenerate all
  language bindings in one change.
- Add CI checks that generated files are reproducible and that GraphQL generated
  output matches a fresh introspection snapshot.
- Keep generated code in dedicated packages; wrappers should depend on the
  generated package, never the reverse.
- Add compatibility tests for optional protobuf fields, status-code mapping,
  metadata authentication, and streaming cancellation.
- Do not silently regenerate bindings during a package build; generation must
  be an explicit, reviewable step.

## Alternatives considered

### Hand-written clients for every protocol

Rejected as the default. It gives idiomatic APIs quickly, but duplicates wire
contracts across three languages and makes proto/schema drift easy to miss.
Hand-written code remains appropriate for the thin wrappers and streaming
lifecycle policy.

### One universal generated SDK

Rejected. Generated code is useful for transport and types, but a universal
surface would make browser, backend, sync, and async usage less idiomatic and
would force unrelated toolchains into every SDK.

### Protocol prioritization without bindings

Rejected. REST remains the compatibility baseline, but the server already
exposes useful gRPC, GraphQL, WebSocket, and SSE capabilities. The POC proves
that the first binding can be exercised now instead of deferring the decision.
