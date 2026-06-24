---
title: "Add Webhook Delivery Retry Queue with Exponential Backoff and Dead-Letter Storage"
labels: ["difficulty: intermediate", "area: infrastructure", "type: feature"]
assignees: []
---

## Summary
Webhook deliveries to downstream consumers currently fail silently if the consumer endpoint returns a non-2xx response or times out. A retry queue with exponential backoff (3 attempts: 30s, 5m, 30m) and dead-letter storage for permanently failed deliveries ensures no alert event is silently dropped.

## Objectives
- [ ] Implement `WebhookRetryQueue` in `api/webhook_sender.py` using `asyncio` task scheduling
- [ ] On delivery failure: schedule retry at 30s, then 5m, then 30m
- [ ] After 3 failed retries: write to `webhook_dlq` SQLite table and emit `webhook.dead_lettered` log event
- [ ] `GET /admin/webhooks/dlq` lists dead-lettered deliveries; `POST /admin/webhooks/dlq/{id}/retry` manually retries one
- [ ] Include HMAC signature on retried deliveries (same `X-LedgerLens-Signature` header)

## Definition of Done
- [ ] Delivery failure triggers 3 retries on the correct schedule
- [ ] Dead-lettered deliveries persist and are visible via admin API
- [ ] Manual retry via admin API succeeds for a temporarily-unavailable consumer
- [ ] Tests mock consumer endpoint failures and verify retry schedule
