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
| `config_change` | `{"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}` | Live settings update via `SettingsReloader` + atomic `.env` write + propagation to every process (see below) |
| `committee_update` | `{"action": "add"\|"remove", "member": "alice@example.com"}` | Insert/soft-delete row in `governance_committee` |

## Configuration propagation

A `config_change` proposal reaching `status='executed'` must actually change the value every live process's scoring, alerting, and counterfactual-explanation logic uses -- not just the process that executed it, and not only after an uncoordinated future restart. This section describes how, and the exact guarantee it makes.

### Consistency model: eventually consistent

There is no synchronous "wait for every replica to acknowledge" step before a proposal is marked `executed` -- doing so would require a replica-discovery/acknowledgement protocol this codebase doesn't otherwise have, for a governance action that already has a 72-hour voting period; a few seconds of extra propagation latency is immaterial in that context. Instead:

- **Durable source of truth**: `execute_proposal` writes the new value to the `runtime_config` SQLite table (same database as everything else), on the *same* `BEGIN EXCLUSIVE` connection/transaction used for the `status='executed'` transition -- the write and the status flip are atomic together. If the write fails, the proposal is marked `failed` (with `execution_error` populated) rather than `executed` without actually propagating.
- **Canonical read path**: every real consumer of a governed setting (`run_pipeline.py`, `detection/alert_engine.py`, `api/main.py`, `detection/counterfactual_engine.py`) reads through `config.settings.get_runtime_risk_score_threshold()`, never `settings.risk_score_threshold` directly. That function polls `runtime_config` through a local, per-process TTL cache (`RUNTIME_CONFIG_TTL_SECONDS`, default 60s).
- **Worst-case bound**: with no other infrastructure configured, every process is guaranteed to observe an executed proposal within `RUNTIME_CONFIG_TTL_SECONDS` (default 60s) of execution -- a hard, documented ceiling, not "eventually, someday."
- **Fast path (Redis configured and reachable)**: `execute_proposal` also bumps a shared Redis counter (`bump_config_version`, default key `ledgerlens:config:version`) after committing. Every process's next config read compares its last-seen counter value against the shared one -- one cheap Redis `GET`, not a new poll cycle -- and re-reads `runtime_config` immediately if the counter moved, regardless of remaining local TTL. In practice this means propagation completes on the very next scoring call / API request / ingestion batch in any process, typically well under a second.
- **Graceful degradation**: when `REDIS_URL` is unset or Redis is unreachable, the fast path silently no-ops (logged once) and every process falls back to the TTL bound above -- identical to this mechanism's pre-existing behavior with no Redis configured at all, including local `docker-compose up` (no `--profile`), which does not run Redis by default.

This is a *versioned-config-epoch* pattern, not literal pub/sub: no background subscriber thread is needed, which matters because `run_pipeline.py` and CLI batch jobs are not long-running daemons and could never host a subscriber loop anyway.

`PATCH /admin/config` (`api/admin_router.py`) uses the exact same mechanism (`invalidate_runtime_config_cache()` + `bump_config_version()` after its own `runtime_config` write) -- it is not deprecated, and its changes propagate identically to a governance proposal's.

### Observability: confirming propagation completed

`GET /health` (`api/main.py`) reports this process's currently-active governed config under `status["config"]`:

```json
{
  "config": {
    "risk_score_threshold": 75,
    "risk_score_threshold_version": "2026-07-22T18:09:17.265672+00:00"
  }
}
```

`risk_score_threshold_version` is the `runtime_config` row's `updated_at` timestamp (`null` if no override has ever been written, i.e. the process is still running the env-configured default). Operators can poll `/health` across replicas after executing a proposal to confirm every process has actually picked it up, rather than assuming propagation succeeded.

### Executing a proposal

```
POST /v1/governance/proposals/{id}/execute      # admin key required
```

Delegates to `GovernanceEngine.execute_proposal`. Returns the proposal's resulting status (`executed` or `failed`, with `execution_error` populated in the latter case) -- `422` if the proposal doesn't exist or isn't in `passed` status.

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
- `execute_proposal` uses `BEGIN EXCLUSIVE` to prevent concurrent execution races. `SettingsReloader.apply()` itself must never open a second SQLite connection while this transaction is held -- a prior version's separate `runtime_config` write inside `apply()` deadlocked against the exclusive lock on every real execution, silently swallowed by an overly broad `except Exception: pass`; the write now happens on `execute_proposal`'s own connection instead (see "Configuration propagation" above).
- Committee member identity is validated against `governance_committee` only (table-based, not cryptographic). Production deployments should add JWT or Stellar keypair signature verification on proposer/voter fields.
- `detection/storage.py` migration 19 fixes `governance_proposals`' schema (migration 7's version predates `GovernanceEngine` and was never compatible with it -- every real column `GovernanceEngine` needs was missing, so `submit_proposal` raised `OperationalError` against any database that had run `init_db()`, i.e. every real deployment) and adds the `governance_votes`/`governance_committee` tables, which no migration created before. Existing databases pick this up automatically the next time `init_db()`/`migrate_db()` runs.

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
