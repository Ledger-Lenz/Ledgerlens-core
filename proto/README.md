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

## Compatibility policy

`proto/ledgerlens` is the gRPC contract consumed by the TypeScript, Go, and
Rust SDKs, so changes must stay backward compatible within a major package
version (`ledgerlens.v1`).

- **Allowed:** adding new messages, RPCs, enum values, and new fields at the
  next unused field number.
- **Breaking (blocked by CI):** removing or renaming fields, messages, RPCs,
  or enum values; changing a field's type, number, or cardinality; reusing a
  field number. Retire fields with `reserved` instead of deleting them.
- Genuinely incompatible changes belong in a new package version
  (e.g. `ledgerlens/v2`) rather than editing `v1` in place.

### CI enforcement

The [`Proto Breaking Change Check`](../.github/workflows/proto-breaking.yml)
workflow runs `buf breaking` (rules: `WIRE_JSON`, configured in
[`buf.yaml`](buf.yaml)) on every PR touching `proto/`, comparing against the
last release tag (`v*`), or the PR base branch if no release exists yet. Run
it locally with:

```bash
buf breaking proto --against ".git#ref=main,subdir=proto"
```

### Intentional breaking changes

A breaking change passes CI only with an explicit, reviewable override. The PR
must have **both**:

1. the `proto-breaking-change` label, and
2. a non-empty section in the PR description:

   ```markdown
   ## Proto breaking change justification
   Why the break is necessary, which SDKs are affected, and the migration plan.
   ```

Reviewers must confirm the justification and that all SDKs are updated before
approving.

## Further reading

See [docs/grpc_scoring.md](../docs/grpc_scoring.md) for the full gRPC
service documentation, including how it compares to the REST API,
authentication, and usage examples.
