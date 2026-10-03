# Cross-SDK conformance suite

The TypeScript (`sdk/`), Go (`go/`), and Rust (`crates/ledgerlens-sdk`) SDKs
implement the same client contract. This suite runs one language-agnostic
fixture set against a shared reference server so behavioural drift (request
shape, query parameters, error-status mapping, response parsing) fails CI.

| File | Purpose |
| --- | --- |
| `cases.json` | The shared fixtures — the single source of truth. |
| `reference_server.py` | Stdlib-only mock API that serves the fixtures. |
| `sdk/tests/conformance.test.ts` | TypeScript runner. |
| `go/conformance_test.go` | Go runner. |
| `crates/ledgerlens-sdk/tests/conformance_test.rs` | Rust runner. |

CI: [`.github/workflows/conformance.yml`](../../../.github/workflows/conformance.yml)
runs all three runners on every change to an SDK or to this directory.

## How it works

Each case describes an SDK `operation` plus `args`, the exact `request` the
SDK must send (method, path, decoded query), the canned `response` the
server returns, and the normalised result every SDK must produce:

- success: `{"ok": {...}}` — the operation-specific projection of the result
- API error: `{"error": {"status": <http status>}}`

Runners `POST /__conformance/select?case=<id>`, invoke the SDK, normalise the
outcome, and compare it to `expect`. If an SDK sends a different request, the
server replies `418` with the expected vs. actual request, so the case fails.
Paths are accepted with or without the `/v1` prefix, matching the real API.

## Running locally

```bash
python tests/contract/conformance/reference_server.py 8787 &
export LEDGERLENS_CONFORMANCE_URL=http://127.0.0.1:8787
(cd sdk && npx vitest run tests/conformance.test.ts)
(cd go && go test -run TestConformance ./...)
cargo test -p ledgerlens-sdk --test conformance_test
```

Runners skip when `LEDGERLENS_CONFORMANCE_URL` is unset.

## Adding a conformance case when the API changes

1. Add a case to `cases.json` with a unique `id`. Reuse an existing
   `operation` if possible.
2. For a new `operation`, add a branch to **all three** runners that calls the
   equivalent SDK method and projects the result into the same `ok` shape.
3. Run the suite locally for all three SDKs. If one fails, fix that SDK;
   do not change the fixture to fit a single SDK.
4. Put the fixture change and every SDK fix in the same PR.

Currently covered: `health` and `list_scores` (including query filtering and
401/404/429/5xx status mapping). `rings` is not covered yet because the Rust
`Ring` model uses a different shape from the TypeScript and Go SDKs. That
needs to be reconciled before it can be added.
