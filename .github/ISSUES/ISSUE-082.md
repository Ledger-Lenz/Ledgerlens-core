---
title: "Implement WebSocket Push Channel for Real-Time Risk Score Alerts"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary
Downstream consumers currently poll `GET /alerts` on a schedule to receive new high-risk detections, creating latency and unnecessary load. A WebSocket endpoint at `ws://host/ws/alerts` pushes alert events to subscribed clients in real time, enabling sub-second notification pipelines for exchange risk management systems.

## Objectives
- [ ] Implement `GET /ws/alerts` WebSocket endpoint using FastAPI's `WebSocket` support
- [ ] Clients authenticate via `?api_key=` query param on connection
- [ ] Publish alert events to connected clients when a new `RiskScore` exceeds the alert threshold
- [ ] Support optional `?wallet_filter=G...` to subscribe to alerts for a specific wallet only
- [ ] Implement heartbeat ping/pong every 30 seconds to detect stale connections
- [ ] Gracefully close connections on server shutdown (drain in-flight messages)

## Definition of Done
- [ ] WebSocket connection receives alert within 100ms of score computation
- [ ] Stale connections dropped after 60s without pong
- [ ] Max 100 concurrent WebSocket connections per server instance (configurable)
- [ ] Integration test demonstrates alert delivery end-to-end
