---
title: "Build Governance Proposal Engine with Full On-Chain-Style Voting Lifecycle"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/governance.py` to implement a full proposal lifecycle: `submit → voting period (72h) → quorum check (>50% of committee) → execute (apply config change or committee membership update)`. Store proposals and votes in SQLite; execute approved proposals atomically via settings reload. This replaces the stub governance mechanism referenced in `docs/governance_protocol.md` with a production-grade, auditable governance engine that controls runtime configuration changes and committee membership.

## Background & Context

LedgerLens includes an off-chain dispute and governance mechanism described in `docs/governance_protocol.md`. The existing implementation is a stub — proposals can be submitted and stored, but there is no enforcement of voting periods, quorum thresholds, or automated execution. This creates a governance gap: configuration changes (e.g., raising `RISK_SCORE_THRESHOLD`, adding a committee member) are applied directly to `.env` without committee oversight.

The governance engine to be built mirrors the semantics of on-chain governance (analogous to Compound Governor Bravo or OpenZeppelin Governor) but is implemented off-chain in Python with SQLite persistence. This is appropriate for the current phase of LedgerLens development; migration to on-chain Soroban governance is a future milestone.

Key design requirements:
- **Proposals** are typed: `ConfigChange` (modify a runtime setting) or `CommitteeUpdate` (add/remove a committee member).
- **Voting period**: 72 hours after submission. Votes submitted after this period are rejected.
- **Quorum**: >50% of current committee size must vote `for` for a proposal to pass.
- **Execution**: approved proposals are applied atomically — either fully executed or rolled back on error. Config changes trigger a live settings reload without service restart.
- **Audit trail**: every proposal, vote, and execution event is written to SQLite with full timestamps.

## Objectives

- [ ] Define `Proposal` dataclass with fields: `id`, `proposal_type` (`config_change` | `committee_update`), `payload` (JSON), `proposer`, `status` (`pending`, `active`, `passed`, `rejected`, `executed`, `failed`), `submitted_at`, `voting_ends_at`, `executed_at`.
- [ ] Define `Vote` dataclass: `proposal_id`, `voter`, `decision` (`for` | `against` | `abstain`), `cast_at`.
- [ ] Implement `GovernanceEngine.submit_proposal(proposer, proposal_type, payload) -> Proposal`.
- [ ] Implement `GovernanceEngine.cast_vote(proposal_id, voter, decision) -> Vote` — validates voter is a committee member, voting period is open, and voter has not already voted.
- [ ] Implement `GovernanceEngine.tally_proposal(proposal_id) -> TallyResult` — computes for/against/abstain counts; determines pass/fail based on quorum rule.
- [ ] Implement `GovernanceEngine.execute_proposal(proposal_id)` — for `config_change`: update `config/settings.py` runtime values atomically; for `committee_update`: add/remove member from the committee table.
- [ ] Implement `GovernanceEngine.close_expired()` — scans `active` proposals past `voting_ends_at` and tallies/closes them; designed to be called by a background task or CLI command.
- [ ] Add `cli.py governance-close-expired` command that calls `close_expired()`.
- [ ] Add REST endpoints: `POST /governance/proposals`, `GET /governance/proposals`, `POST /governance/proposals/{id}/vote`, `GET /governance/proposals/{id}`, `POST /governance/proposals/{id}/execute` (admin-key gated for execute).
- [ ] All endpoints and engine methods covered by tests; ≥90% branch coverage.

## Technical Requirements

### Dataclasses

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Literal
import json

@dataclass
class Proposal:
    id: Optional[int]
    proposal_type: Literal["config_change", "committee_update"]
    payload: dict           # {"key": "RISK_SCORE_THRESHOLD", "new_value": 75} or
                            # {"action": "add"|"remove", "member": "alice@example.com"}
    proposer: str           # committee member identifier
    status: str             # pending | active | passed | rejected | executed | failed
    submitted_at: datetime
    voting_ends_at: datetime    # submitted_at + 72h
    executed_at: Optional[datetime] = None
    execution_error: Optional[str] = None

@dataclass
class Vote:
    id: Optional[int]
    proposal_id: int
    voter: str
    decision: Literal["for", "against", "abstain"]
    cast_at: datetime

@dataclass
class TallyResult:
    proposal_id: int
    for_count: int
    against_count: int
    abstain_count: int
    committee_size: int
    quorum_required: int        # ceil(committee_size * 0.5) + 1
    quorum_met: bool
    outcome: Literal["passed", "rejected"]
```

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS governance_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_type   TEXT NOT NULL,
    payload         TEXT NOT NULL,      -- JSON
    proposer        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    submitted_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    voting_ends_at  TIMESTAMP NOT NULL,
    executed_at     TIMESTAMP,
    execution_error TEXT
);

CREATE TABLE IF NOT EXISTS governance_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     INTEGER NOT NULL REFERENCES governance_proposals(id),
    voter           TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK(decision IN ('for', 'against', 'abstain')),
    cast_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proposal_id, voter)   -- one vote per member per proposal
);

CREATE TABLE IF NOT EXISTS governance_committee (
    member          TEXT PRIMARY KEY,
    added_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active          INTEGER NOT NULL DEFAULT 1
);
```

### `GovernanceEngine` interface

```python
class GovernanceEngine:
    VOTING_PERIOD_HOURS = 72
    QUORUM_FRACTION = 0.5       # strict majority

    def __init__(self, db_path: str, settings_reloader: "SettingsReloader"):
        ...

    def submit_proposal(self, proposer: str, proposal_type: str, payload: dict) -> Proposal:
        """Validate proposer is committee member. Insert with status='active'."""
        ...

    def cast_vote(self, proposal_id: int, voter: str, decision: str) -> Vote:
        """
        Validate: voter is active committee member; proposal is 'active';
        voting_ends_at > now; voter has not voted.
        Raises GovernanceVoteError on any violation.
        """
        ...

    def tally_proposal(self, proposal_id: int) -> TallyResult:
        """
        Count for/against/abstain. quorum_required = floor(committee_size/2) + 1.
        Does NOT change proposal status — call close_proposal() separately.
        """
        ...

    def close_proposal(self, proposal_id: int) -> Proposal:
        """Tally and set status to 'passed' or 'rejected'. Idempotent after closure."""
        ...

    def execute_proposal(self, proposal_id: int) -> Proposal:
        """
        Execute a 'passed' proposal.
        config_change: call settings_reloader.apply(key, new_value) atomically.
        committee_update: insert/soft-delete committee row.
        On success: status='executed', executed_at=now.
        On error: status='failed', execution_error=str(e). Never leaves partial state.
        """
        ...

    def close_expired(self) -> list[Proposal]:
        """Close all active proposals past voting_ends_at. Returns list of closed proposals."""
        ...
```

### `SettingsReloader` for atomic config change

```python
class SettingsReloader:
    ALLOWED_SETTINGS = {"RISK_SCORE_THRESHOLD", "SOROBAN_CIRCUIT_BREAKER_THRESHOLD",
                        "FEEDBACK_DECAY_LAMBDA", "CROSS_CHAIN_MIN_CONFIDENCE"}

    def apply(self, key: str, new_value: str) -> None:
        """
        Validate key is in ALLOWED_SETTINGS. Parse new_value to correct type.
        Apply to the live settings object. Write to .env file atomically
        (write to .env.tmp, then os.replace(.env.tmp, .env)).
        Raises ValueError for disallowed keys or unparseable values.
        """
        ...
```

### API endpoints

```python
@router.post("/governance/proposals", response_model=ProposalOut, status_code=201)
async def submit_proposal(body: ProposalSubmission, ...): ...

@router.get("/governance/proposals", response_model=List[ProposalOut])
async def list_proposals(status: Optional[str] = None, page: int = 1, ...): ...

@router.post("/governance/proposals/{proposal_id}/vote", response_model=VoteOut)
async def cast_vote(proposal_id: int, body: VoteSubmission, ...): ...

@router.get("/governance/proposals/{proposal_id}", response_model=ProposalDetailOut)
async def get_proposal(proposal_id: int, ...): ...

@router.post("/governance/proposals/{proposal_id}/execute", response_model=ProposalOut)
async def execute_proposal(proposal_id: int, admin_key: str = Header(...), ...): ...
```

## Security Considerations

- **Allowed settings whitelist**: `SettingsReloader.ALLOWED_SETTINGS` must be a compile-time constant. A governance proposal attempting to change `LEDGERLENS_SERVICE_SECRET_KEY` or `LEDGERLENS_ADMIN_API_KEY` must be rejected with `GovernanceError("Setting not modifiable via governance")`.
- **Atomic `.env` write**: use `os.replace` (atomic on POSIX) to prevent a corrupted `.env` if the process is killed mid-write. Never write the secret key values to `.env` via governance — only non-secret config values are in `ALLOWED_SETTINGS`.
- **Committee member authentication**: the current implementation uses string identifiers for committee members (`proposer`, `voter`). In the MVP, these are validated against the `governance_committee` table only — not cryptographically authenticated. Document this limitation explicitly and note that production deployments should add request signing (e.g., JWT or Stellar keypair signatures) to committee member actions.
- **Vote deduplication**: the `UNIQUE(proposal_id, voter)` constraint in SQLite enforces one-vote-per-member at the database layer, not just the application layer.
- **Execution idempotency**: `execute_proposal` must check that status is `passed` before executing. Concurrent calls must be safe; use a `SELECT ... FOR UPDATE` equivalent (SQLite `BEGIN EXCLUSIVE` transaction).

## Testing Requirements

- **Unit — `submit_proposal`**: non-committee proposer → raises `GovernanceError`; valid proposer → returns `Proposal` with `voting_ends_at = submitted_at + 72h`.
- **Unit — `cast_vote` validations**: not a committee member → error; proposal expired → error; duplicate vote → error; valid vote → persisted.
- **Unit — `tally_proposal` quorum**: 5-member committee, 3 `for` votes → quorum met; 2 `for` votes → quorum not met.
- **Unit — `close_expired` timing**: advance mock clock past `voting_ends_at`; assert `close_expired()` returns the proposal with updated status.
- **Unit — `execute_proposal` config change**: mock `SettingsReloader.apply`; assert called with correct key/value; assert proposal status → `executed`.
- **Unit — `execute_proposal` failure**: mock `SettingsReloader.apply` raising `ValueError`; assert proposal status → `failed`, `execution_error` populated.
- **Unit — disallowed settings key**: governance proposal to change `LEDGERLENS_SERVICE_SECRET_KEY`; assert `GovernanceError` raised before any write.
- **Integration — full lifecycle**: submit → cast 3 votes → tally → execute; assert proposal status sequence is `active → passed → executed`.
- **Integration — API 422 on expired vote**: vote on proposal past deadline → 422.

## Documentation Requirements

- Docstrings on all `GovernanceEngine` methods.
- Update `docs/governance_protocol.md` to reflect the implemented lifecycle (replacing the stub description).
- Add governance API endpoints to the README API table.
- Document `cli.py governance-close-expired` in CLI Reference.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `Proposal`, `Vote`, `TallyResult` dataclasses implemented.
- [ ] `GovernanceEngine` fully implemented: submit, vote, tally, close, execute, close_expired.
- [ ] `SettingsReloader` with whitelist and atomic `.env` write implemented.
- [ ] SQLite tables created via `db-migrate`.
- [ ] All REST endpoints operational; execute endpoint is admin-key gated.
- [ ] `cli.py governance-close-expired` command implemented.
- [ ] Concurrent execute safety (exclusive transaction) implemented.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/governance_protocol.md` updated.
- [ ] `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience designing and implementing state-machine-based workflow engines — governance, approval workflows, or similar lifecycle systems — in Python. Familiarity with on-chain governance patterns (Compound Governor, OpenZeppelin) is a significant advantage even though this is an off-chain implementation. You are comfortable with SQLite transaction semantics and atomic file operations on POSIX systems. Understanding of LedgerLens's risk score pipeline and why config parameters like `RISK_SCORE_THRESHOLD` require governance oversight will help you design robust safeguards.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., governance systems, workflow engines, Python backend, on-chain governance).
2. **Relevant experience**: proposal/approval workflow engines, on-chain governance implementations, or similar systems you have built.
3. **Approach / thoughts**: how would you handle the race condition where two committee members submit competing proposals to change the same setting simultaneously?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
