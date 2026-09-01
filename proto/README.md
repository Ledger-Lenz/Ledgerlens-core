# proto/

Protocol Buffer service definitions for LedgerLens's internal gRPC scoring
service.

## Layout

- `ledgerlens/v1/scoring.proto` — defines `ScoringService`, the
  low-latency gRPC alternative to the REST scoring API. It declares the
  `ScoreRequest` / `RiskScoreProto` messages and the unary (`ScoreWallet`)
  and bidirectional-streaming (`BatchScoreWallets`) RPCs.

## Code generation

The Python bindings compiled from these `.proto` files live in
[`generated/`](../generated/) (`scoring_pb2.py`, `scoring_pb2_grpc.py`,
`scoring_pb2.pyi`). Regenerate them with `grpc_tools.protoc` whenever
`scoring.proto` changes.

## Further reading

See [docs/grpc_scoring.md](../docs/grpc_scoring.md) for the full gRPC
service documentation, including how it compares to the REST API,
authentication, and usage examples.
