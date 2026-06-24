---
title: "Add Python SDK Client Library for LedgerLens API with Typed Pydantic Models"
labels: ["difficulty: intermediate", "area: sdk", "type: feature"]
assignees: []
---

## Summary
Exchange engineers integrating LedgerLens into their risk systems must write their own HTTP client code against the REST API. A first-party Python SDK (`ledgerlens`) with typed request/response models, automatic retry, and async support reduces integration time from days to hours.

## Objectives
- [ ] Create `packages/ledgerlens-sdk/` as a standalone Python package
- [ ] Implement `LedgerLensClient(base_url, api_key)` with methods mirroring all public API endpoints
- [ ] All responses typed as Pydantic v2 models; raise `LedgerLensAPIError` on non-2xx responses
- [ ] Async client: `AsyncLedgerLensClient` using `httpx.AsyncClient`
- [ ] Publish to PyPI as `ledgerlens-sdk`; version synced with API version

## Definition of Done
- [ ] `from ledgerlens import LedgerLensClient; client.get_score("G...")` works against a live server
- [ ] Async client tested with `asyncio.gather` for concurrent scoring
- [ ] SDK docs auto-generated from docstrings via pdoc
- [ ] Integration test runs SDK against a local test server
