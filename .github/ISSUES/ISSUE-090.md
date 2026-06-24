---
title: "Add Comprehensive End-to-End Integration Test Suite with Testcontainers"
labels: ["difficulty: advanced", "area: testing", "type: feature"]
assignees: []
---

## Summary
The current test suite uses unit tests with mocked dependencies, which cannot catch integration failures between the API, feature store, and scoring pipeline. An end-to-end test suite using Testcontainers to spin up real SQLite/Redis containers runs the full request path — from API call through feature extraction to score storage — giving confidence that components work together correctly.

## Objectives
- [ ] Create `tests/e2e/` directory with Testcontainers-based fixtures for the full LedgerLens stack
- [ ] E2E test: ingest synthetic trade batch → score wallet → `GET /scores/{wallet}` returns expected score
- [ ] E2E test: high-risk score → alert fires → `GET /alerts` returns the alert
- [ ] E2E test: federated training round completes without error (using mock exchange client)
- [ ] Run E2E suite in CI as a separate job (`make test-e2e`) after unit tests pass
- [ ] E2E suite must complete in < 5 minutes

## Definition of Done
- [ ] 10+ E2E test cases covering the core detection pipeline
- [ ] All E2E tests pass in CI on ubuntu-latest
- [ ] E2E suite is isolated: leaves no persistent state between test runs
- [ ] A real SQLite file (not in-memory) is used to catch file-locking and WAL issues
