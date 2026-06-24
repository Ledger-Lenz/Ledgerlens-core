---
title: "Implement Model Shadow Mode Deployment for Parallel New-vs-Current Scoring"
labels: ["difficulty: advanced", "area: ml", "type: feature"]
assignees: []
---

## Summary
Promoting a newly trained model to production is currently a hard cutover with no ability to compare new vs. current scores before committing. Shadow mode runs the new model in parallel with the production model on every live scoring request, logging score divergence without affecting API responses — enabling data-driven promotion decisions based on real traffic.

## Objectives
- [ ] Add `SHADOW_MODEL_VERSION` env var; when set, load a second model alongside the production model
- [ ] For each scoring request, compute both the production and shadow scores asynchronously
- [ ] Log `shadow_score_divergence` (absolute difference) to Prometheus histogram metric
- [ ] Store shadow scores in `shadow_scores` SQLite table for offline analysis
- [ ] `GET /admin/shadow/report` returns: mean divergence, p95 divergence, wallets with divergence > 20

## Definition of Done
- [ ] Shadow scoring adds < 10ms overhead to p99 API latency (async parallel execution)
- [ ] Divergence metric visible in Prometheus after 100 requests
- [ ] Shadow scores do not appear in `GET /scores/{wallet}` response (production score only)
- [ ] Tests verify shadow model runs for every request when configured
