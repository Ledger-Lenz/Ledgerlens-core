# Roadmap

This tracks near-term direction for `ledgerlens-core`. It complements, rather
than replaces, the per-feature docs under `docs/` and the cross-repo contracts
documented in the [README's "LedgerLens Organization" section](README.md#ledgerlens-organization).

## Now

- **Restore automated releases.** `.github/workflows/release-please.yml` was
  removed; `CHANGELOG.md` and the `pyproject.toml` version are currently
  bumped by hand. Reinstate release-please (or an equivalent) so tagging,
  changelog generation, and the GHCR image publish stay in sync automatically.
- **Triage the `.github/ISSUES/` backlog.** Several issues in that directory
  (SDKs, audit log, multi-tenant namespaces, API key management, chaos
  testing, GNN ring detection, temporal patterns, streaming, tracing) already
  appear implemented in the current tree (`sdk/`, `packages/`, `audit/`,
  `api/namespace.py`, `api/api_key_router.py`, `tests/chaos/`,
  `detection/gnn_ring_detector.py`, `detection/temporal_patterns.py`,
  `api/streaming_router.py`, `detection/tracing.py`). Close or update the
  stale ones so the backlog reflects real open work.

## Next

- **Dynamic asset thresholds**: Replace the hardcoded `BENFORD_MAD_THRESHOLD=0.015` with a per-asset-pair dynamic threshold learned over a 30-day trailing window. Highly liquid pairs (XLM/USDC) have much tighter distributions than long-tail tokens; applying the same MAD cutoff across the board is driving up false positives on low-volume pairs.
- **Federated learning coordinator**: Shift the `federated_dp_epsilon` / `federated_dp_delta` tracking out of the core pipeline into a dedicated coordinator service. The current design requires running the pipeline in "coordinator mode", which mixes concerns and complicates the deployment footprint.
- **Cross-chain wash detection**: Integrate the `evm_loader` data with the existing `horizon_streamer` data to detect wash rings that route capital across the bridge to obscure the circular flow.

## Later

- Expand the GNN ring detector and federated learning paths beyond their
  current scope (see `docs/gnn_ring_detection.md`, `docs/federated_learning.md`)
  as production usage surfaces gaps.

## How to use this file

When a roadmap item ships, move its CHANGELOG entry to a dated release
section and delete the line here. When new cross-repo work is identified,
add it here rather than letting it live only in a chat thread or PR
description.
