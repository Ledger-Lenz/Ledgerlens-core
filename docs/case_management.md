# Analyst Case Management

## Overview

The case-management layer (Issue #200 follow-up) prevents multiple analysts from independently reviewing the same wallet by introducing explicit **claim/release/resolve** lifecycle with soft locking and SLA visibility.

## Lifecycle

```
  ┌─────────────┐
  │   Unclaimed  │  ← wallet appears in queue
  └──────┬──────┘
         │ POST /analyst/wallet/{wallet}/claim
         ▼
  ┌─────────────┐
  │   Claimed    │  ← assigned to one analyst, soft-locked
  └──────┬──────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐ ┌────────┐
│Resolve │ │Release │  ← analyst gives up or submits verdict
│(verdict)│ │(early) │
└────────┘ └────────┘
    │         │
    └────┬────┘
         ▼
  ┌─────────────┐
  │  Unclaimed   │  ← back in queue for others
  └─────────────┘
```

### Auto-Release

Claims expire after `ANALYST_LOCK_TIMEOUT_SECONDS` (default 1800s / 30 min). A background sweep (`python cli.py analyst-lock-sweep`) automatically releases expired locks.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analyst/wallet/{wallet}/claim` | Claim a wallet for review (soft lock) |
| `POST` | `/analyst/wallet/{wallet}/release` | Release a claim before verdict |
| `POST` | `/analyst/wallet/{wallet}/feedback` | Submit verdict (requires active claim) |
| `GET` | `/analyst/queue` | Queue with assignment annotations |
| `GET` | `/analyst/case-stats` | SLA and case-management metrics |

### Claim

```http
POST /analyst/wallet/GABC.../claim?asset_pair=XLM/USDC
Content-Type: application/json
X-LedgerLens-Admin-Key: <key>

{"analyst_key_hash": "a1b2c3d4e5f6"}
```

**200 OK** — wallet claimed:
```json
{
  "wallet": "GABC...",
  "asset_pair": "XLM/USDC",
  "analyst_key_hash": "a1b2c3d4e5f6",
  "assigned_at": "2026-07-17T10:00:00Z",
  "lock_expires_at": "2026-07-17T10:30:00Z"
}
```

**409 Conflict** — already claimed by another analyst:
```json
{
  "detail": "Already claimed",
  "assigned_to": "d4e5f6...",
  "lock_expires_at": "2026-07-17T10:22:00Z"
}
```

**429 Too Many Requests** — analyst has reached the concurrent claim cap.

### Release

```http
POST /analyst/wallet/GABC.../release
Content-Type: application/json

{"analyst_key_hash": "a1b2c3d4e5f6"}
```

### Feedback (now requires claim)

Submitting a verdict without an active claim returns **403 Forbidden**. The claimant must match the submitting analyst.

### Queue annotations

Each queue item now includes:
- `is_assigned` — whether the wallet has an active claim
- `assigned_to` — analyst_key_hash of the claimant (null if unassigned)
- `lock_expires_at` — ISO timestamp of lock expiry (null if unassigned)

### Case Stats

`GET /analyst/case-stats` returns:
- `avg_time_to_claim_seconds` — average time from queue appearance to first claim
- `avg_time_to_resolution_seconds` — average time from claim to verdict
- `assigned_count` — wallets currently assigned
- `unassigned_count` — wallets in queue with no active claim
- `expired_reclaimed_count` — locks released due to expiry

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `ANALYST_LOCK_TIMEOUT_SECONDS` | `1800` | Soft lock duration (seconds) |
| `ANALYST_CLAIM_MAX_ACTIVE_PER_ANALYST` | `10` | Max concurrent claims per analyst |

## Security Note

The current auth model uses a shared `X-LedgerLens-Admin-Key`. True per-analyst accountability requires the per-analyst scoped API key work (Issue #195, `api/api_key_router.py`). The `analyst_key_hash` field in request bodies provides identity tracking but relies on clients self-reporting; server-side enforcement will strengthen once per-analyst keys are available.

## Background Worker

```bash
python cli.py analyst-lock-sweep --interval 60
```

Runs continuously, releasing expired locks every `--interval` seconds (default 60). Should be deployed as a systemd service or cron job alongside the API server.
