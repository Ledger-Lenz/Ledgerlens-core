---
title: "Add Batch Wallet Scoring Endpoint with Async Job Queue"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary
The current `GET /scores/{wallet}` endpoint scores one wallet per request. Compliance teams and exchange risk desks need to score thousands of wallets at once. A `POST /scores/batch` endpoint accepting a list of wallets and returning an async job ID — with `GET /scores/batch/{job_id}` for result polling — enables bulk risk assessment without per-wallet HTTP overhead.

## Objectives
- [ ] `POST /scores/batch` accepts `{"wallets": ["G...", ...], "priority": "normal|high"}` (max 1000 wallets)
- [ ] Returns `{"job_id": "...", "status": "queued", "estimated_seconds": N}`
- [ ] Process batch asynchronously using `asyncio.gather` over the existing scoring pipeline
- [ ] `GET /scores/batch/{job_id}` returns progress and results when complete
- [ ] Store job state in SQLite `batch_jobs` table with TTL of 24 hours
- [ ] Rate limit: max 5 concurrent batch jobs per API key

## Definition of Done
- [ ] Batch of 1000 wallets completes in < 60 seconds on 4-core hardware
- [ ] Job state persists across server restarts
- [ ] Results available for 24 hours after completion
- [ ] Tests cover max-size batch, concurrent job limit, and expired job cleanup
