# Governance Protocol

This document describes the off-chain dispute and governance workflow used by LedgerLens.

## Dispute lifecycle

- A consumer may submit a dispute for a previously published on-chain score via `POST /disputes`.
- Disputes are stored in `score_disputes` and start in `pending` status.
- Committee members cast votes via `POST /disputes/{id}/vote` (admin-key gated in this MVP).
- When quorum and supermajority are reached, the dispute is resolved as `approved` or `rejected`.
- On `approved`, the score is removed from the `risk_scores` table, and a `score = 0` override is published on-chain via the Soroban `submit_score` call. The override is recorded in `score_overrides` for audit.

## Quorum rules

- Committee size is recorded in `committee_members`.
- Default `COMMITTEE_QUORUM` is 3.
- A dispute is resolved when at least `COMMITTEE_QUORUM` votes are cast and a 2/3 supermajority exists for `approve` or `reject`.
- If neither supermajority is reached before `COMMITTEE_VOTE_DEADLINE_DAYS` (default 14), the dispute expires and the score is upheld by default.

## Governance proposals

- Proposals allow changing runtime configuration and managing committee membership.
- Proposal types:
  - `change_threshold` — requires 3/4 supermajority to pass.
  - `add_committee_member` / `remove_committee_member` — simple majority.
- Passing a `change_threshold` updates `runtime_config` and is applied via hot-reload.

## Soroban override mechanism

- Approved disputes trigger a background call to `submit_score(... score=0 ...)` to signal invalidation on-chain.
- Failures are recorded in `score_overrides` and will be retried by background processes.

## SSRF protection for evidence URLs

- `evidence_url` must be HTTPS and is validated with `urllib.parse`.
- URLs pointing to private IP ranges (10.x.x.x, 172.16.x.x, 192.168.x.x, 127.x.x.x) are rejected to reduce SSRF risk.

*** End of document
