# Governance Protocol

This document describes the off-chain governance mechanism for LedgerLens.

## Overview

The governance engine (`detection/governance.py`) implements a full proposal lifecycle — submit → voting period (72 h) → quorum check → execute — persisted in SQLite and applied atomically at runtime.

## Proposal lifecycle

```
submit_proposal()  →  status: active
      ↓  (72 h voting window)
close_proposal()   →  status: passed | rejected
      ↓  (admin executes)
execute_proposal() →  status: executed | failed
```

Expired proposals are closed automatically by `cli.py governance-close-expired` (designed for cron / systemd scheduling).

## Proposal types

| type | payload | effect |
|---|---|---|
| `config_change` | `{"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}` | Live settings update via `SettingsReloader` + atomic `.env` write |
| `committee_update` | `{"action": "add"\|"remove", "member": "alice@example.com"}` | Insert/soft-delete row in `governance_committee` |

## Quorum rule

`quorum_required = floor(committee_size / 2) + 1`

A proposal passes when the number of `for` votes reaches `quorum_required` (strict majority). Abstentions do not count toward quorum.

## REST API

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/governance/proposals` | none | Submit a proposal (proposer must be a committee member) |
| `GET`  | `/governance/proposals` | none | List proposals (filterable by `?status=`) |
| `GET`  | `/governance/proposals/{id}` | none | Get proposal + tally |
| `POST` | `/governance/proposals/{id}/vote` | none | Cast a vote |
| `POST` | `/governance/proposals/{id}/execute` | admin key | Execute a passed proposal |

## CLI

```bash
python cli.py governance-close-expired   # tally and close all expired active proposals
```

## Allowed settings (SettingsReloader.ALLOWED_SETTINGS)

Only these keys may be changed via governance. Secret keys are **never** modifiable this way.

- `RISK_SCORE_THRESHOLD`
- `SOROBAN_CIRCUIT_BREAKER_THRESHOLD`
- `FEEDBACK_DECAY_LAMBDA`
- `CROSS_CHAIN_MIN_CONFIDENCE`

## Security notes

- `SettingsReloader.ALLOWED_SETTINGS` is a compile-time frozenset; governance proposals referencing `LEDGERLENS_SERVICE_SECRET_KEY` or `LEDGERLENS_ADMIN_API_KEY` are rejected before any DB write.
- `.env` is written atomically via `os.replace(.env.tmp → .env)` (POSIX-atomic rename).
- `UNIQUE(proposal_id, voter)` in `governance_votes` enforces one-vote-per-member at the database layer.
- `execute_proposal` uses `BEGIN EXCLUSIVE` to prevent concurrent execution races.
- Committee member identity is validated against `governance_committee` only (table-based, not cryptographic). Production deployments should add JWT or Stellar keypair signature verification on proposer/voter fields.

## Dispute lifecycle

- Submit disputes via `POST /disputes`.
- Committee members vote via `POST /disputes/{id}/vote` (admin-key gated).
- When quorum + 2/3 supermajority reached, dispute is `approved` or `rejected`.
- Approved disputes remove the score locally and publish `score=0` on-chain via Soroban `submit_score`.

## Soroban override mechanism

- Approved disputes trigger a background call to `submit_score(..., score=0)`.
- Failures are recorded in `score_overrides` and retried by background processes.

## SSRF protection for evidence URLs

- `evidence_url` must be HTTPS.
- URLs pointing to private IP ranges are rejected.
