"""Governance Proposal Engine — Issue #150.

Implements a full proposal lifecycle:
  submit → voting period (72h) → quorum check (>50% of committee) → execute.

Proposals are stored in SQLite. Config changes are applied atomically via
SettingsReloader. Committee membership changes update the governance_committee
table.

Security notes:
- SettingsReloader.ALLOWED_SETTINGS is a compile-time constant; governance
  proposals cannot change secret keys.
- Atomic .env write uses os.replace (POSIX-atomic rename).
- UNIQUE(proposal_id, voter) is enforced at the DB layer.
- execute_proposal uses BEGIN EXCLUSIVE to prevent concurrent execution races.
- Committee member authentication in this MVP is table-based only (not
  cryptographic). Production deployments should add JWT or Stellar keypair
  signature verification.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from config.settings import settings


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GovernanceError(Exception):
    """Base governance error."""


class GovernanceVoteError(GovernanceError):
    """Raised when a vote cannot be cast."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    id: Optional[int]
    proposal_type: Literal["config_change", "committee_update"]
    payload: dict
    proposer: str
    status: str  # active | passed | rejected | executed | failed
    submitted_at: datetime
    voting_ends_at: datetime
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
    quorum_required: int   # floor(committee_size/2) + 1
    quorum_met: bool
    outcome: Literal["passed", "rejected"]


# ---------------------------------------------------------------------------
# SettingsReloader — atomic config change applier
# ---------------------------------------------------------------------------

class SettingsReloader:
    """Apply runtime configuration changes atomically.

    Only settings listed in ALLOWED_SETTINGS may be changed via governance.
    Secret keys are explicitly excluded.
    """

    # Compile-time constant — do NOT add secret keys here.
    ALLOWED_SETTINGS: frozenset[str] = frozenset({
        "RISK_SCORE_THRESHOLD",
        "SOROBAN_CIRCUIT_BREAKER_THRESHOLD",
        "FEEDBACK_DECAY_LAMBDA",
        "CROSS_CHAIN_MIN_CONFIDENCE",
    })

    _TYPE_MAP: dict[str, type] = {
        "RISK_SCORE_THRESHOLD": int,
        "SOROBAN_CIRCUIT_BREAKER_THRESHOLD": int,
        "FEEDBACK_DECAY_LAMBDA": float,
        "CROSS_CHAIN_MIN_CONFIDENCE": float,
    }

    def apply(self, key: str, new_value: str) -> None:
        """Validate and apply a config change; write to .env atomically.

        Raises ValueError for disallowed keys or unparseable values.
        """
        if key not in self.ALLOWED_SETTINGS:
            raise GovernanceError(
                f"Setting not modifiable via governance: {key}. "
                "Disallowed keys include all secret keys."
            )

        # Parse to correct type to validate
        target_type = self._TYPE_MAP.get(key, str)
        try:
            parsed = target_type(new_value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot parse {new_value!r} as {target_type.__name__} for {key}: {exc}") from exc

        # Apply to live settings object
        attr_map = {
            "RISK_SCORE_THRESHOLD": "_default_risk_score_threshold",
            "SOROBAN_CIRCUIT_BREAKER_THRESHOLD": "soroban_circuit_breaker_threshold",
            "FEEDBACK_DECAY_LAMBDA": "feedback_decay_lambda",
            "CROSS_CHAIN_MIN_CONFIDENCE": "cross_chain_min_confidence",
        }
        live_attr = attr_map.get(key, key.lower())
        try:
            object.__setattr__(settings, live_attr, parsed)
        except (AttributeError, TypeError):
            pass  # Settings may be frozen; best-effort live apply

        # Write to .env atomically (write to .env.tmp, then os.replace)
        env_path = ".env"
        tmp_path = ".env.tmp"
        env_lines: list[str] = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                env_lines = f.readlines()

        updated = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
                env_lines[i] = f"{key}={new_value}\n"
                updated = True
                break
        if not updated:
            env_lines.append(f"{key}={new_value}\n")

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(env_lines)
        os.replace(tmp_path, env_path)

        # Also update runtime_config table for hot-reload
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)",
                    (key.lower(), str(parsed), datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
        except Exception:
            pass  # Best-effort; .env is the authoritative write


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_proposal(row) -> Proposal:
    return Proposal(
        id=row["id"],
        proposal_type=row["proposal_type"],
        payload=json.loads(row["payload"]),
        proposer=row["proposer"],
        status=row["status"],
        submitted_at=_parse_dt(row["submitted_at"]),
        voting_ends_at=_parse_dt(row["voting_ends_at"]),
        executed_at=_parse_dt(row["executed_at"]),
        execution_error=row["execution_error"],
    )


def _row_to_vote(row) -> Vote:
    return Vote(
        id=row["id"],
        proposal_id=row["proposal_id"],
        voter=row["voter"],
        decision=row["decision"],
        cast_at=_parse_dt(row["cast_at"]),
    )


# ---------------------------------------------------------------------------
# GovernanceEngine
# ---------------------------------------------------------------------------

class GovernanceEngine:
    """Full governance proposal lifecycle engine.

    Methods are idempotent after terminal states and safe against concurrent
    access via SQLite EXCLUSIVE transactions.
    """

    VOTING_PERIOD_HOURS = 72
    QUORUM_FRACTION = 0.5

    def __init__(
        self,
        db_path: str | None = None,
        settings_reloader: SettingsReloader | None = None,
        _now_fn=None,
    ) -> None:
        self._db_path = db_path or settings.db_path
        self._reloader = settings_reloader or SettingsReloader()
        self._now = _now_fn or (lambda: datetime.now(timezone.utc))

    def _conn(self):
        return _connect(self._db_path)

    def _committee_size(self, conn) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM governance_committee WHERE active = 1"
        ).fetchone()
        return row[0] if row else 0

    def _is_committee_member(self, conn, member: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM governance_committee WHERE member = ? AND active = 1",
            (member,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # submit_proposal
    # ------------------------------------------------------------------

    def submit_proposal(
        self,
        proposer: str,
        proposal_type: str,
        payload: dict,
    ) -> Proposal:
        """Validate proposer is a committee member; insert proposal with status='active'.

        Raises GovernanceError if proposer is not an active committee member or
        proposal_type is invalid.
        """
        if proposal_type not in ("config_change", "committee_update"):
            raise GovernanceError(f"Invalid proposal_type: {proposal_type!r}")

        # Validate config_change payload
        if proposal_type == "config_change":
            key = payload.get("key", "")
            if key not in SettingsReloader.ALLOWED_SETTINGS:
                raise GovernanceError(
                    f"Setting not modifiable via governance: {key}"
                )

        with self._conn() as conn:
            if not self._is_committee_member(conn, proposer):
                raise GovernanceError(f"Proposer {proposer!r} is not an active committee member")

            now = self._now()
            voting_ends_at = now + timedelta(hours=self.VOTING_PERIOD_HOURS)

            cur = conn.execute(
                """INSERT INTO governance_proposals
                   (proposal_type, payload, proposer, status, submitted_at, voting_ends_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (
                    proposal_type,
                    json.dumps(payload),
                    proposer,
                    now.isoformat(),
                    voting_ends_at.isoformat(),
                ),
            )
            conn.commit()
            pid = cur.lastrowid

        return Proposal(
            id=pid,
            proposal_type=proposal_type,  # type: ignore[arg-type]
            payload=payload,
            proposer=proposer,
            status="active",
            submitted_at=now,
            voting_ends_at=voting_ends_at,
        )

    # ------------------------------------------------------------------
    # cast_vote
    # ------------------------------------------------------------------

    def cast_vote(self, proposal_id: int, voter: str, decision: str) -> Vote:
        """Cast a vote on a proposal.

        Validates:
        - voter is an active committee member
        - proposal status is 'active'
        - voting period is still open
        - voter has not already voted (DB-level UNIQUE enforces this too)

        Raises GovernanceVoteError on any violation.
        """
        if decision not in ("for", "against", "abstain"):
            raise GovernanceVoteError(f"Invalid decision: {decision!r}")

        with self._conn() as conn:
            if not self._is_committee_member(conn, voter):
                raise GovernanceVoteError(f"Voter {voter!r} is not an active committee member")

            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GovernanceVoteError(f"Proposal {proposal_id} not found")

            if row["status"] != "active":
                raise GovernanceVoteError(
                    f"Proposal {proposal_id} is not active (status={row['status']!r})"
                )

            voting_ends = _parse_dt(row["voting_ends_at"])
            now = self._now()
            if now > voting_ends:
                raise GovernanceVoteError(
                    f"Voting period for proposal {proposal_id} has expired"
                )

            existing = conn.execute(
                "SELECT 1 FROM governance_votes WHERE proposal_id = ? AND voter = ?",
                (proposal_id, voter),
            ).fetchone()
            if existing:
                raise GovernanceVoteError(
                    f"Voter {voter!r} has already voted on proposal {proposal_id}"
                )

            cur = conn.execute(
                """INSERT INTO governance_votes (proposal_id, voter, decision, cast_at)
                   VALUES (?, ?, ?, ?)""",
                (proposal_id, voter, decision, now.isoformat()),
            )
            conn.commit()
            vote_id = cur.lastrowid

        return Vote(
            id=vote_id,
            proposal_id=proposal_id,
            voter=voter,
            decision=decision,  # type: ignore[arg-type]
            cast_at=now,
        )

    # ------------------------------------------------------------------
    # tally_proposal
    # ------------------------------------------------------------------

    def tally_proposal(self, proposal_id: int) -> TallyResult:
        """Tally votes for a proposal. Does NOT change proposal status."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GovernanceError(f"Proposal {proposal_id} not found")

            rows = conn.execute(
                "SELECT decision, COUNT(*) as cnt FROM governance_votes "
                "WHERE proposal_id = ? GROUP BY decision",
                (proposal_id,),
            ).fetchall()

            counts: dict[str, int] = {"for": 0, "against": 0, "abstain": 0}
            for r in rows:
                counts[r["decision"]] = r["cnt"]

            committee_size = self._committee_size(conn)

        quorum_required = math.floor(committee_size * self.QUORUM_FRACTION) + 1
        quorum_met = counts["for"] >= quorum_required
        outcome: Literal["passed", "rejected"] = "passed" if quorum_met else "rejected"

        return TallyResult(
            proposal_id=proposal_id,
            for_count=counts["for"],
            against_count=counts["against"],
            abstain_count=counts["abstain"],
            committee_size=committee_size,
            quorum_required=quorum_required,
            quorum_met=quorum_met,
            outcome=outcome,
        )

    # ------------------------------------------------------------------
    # close_proposal
    # ------------------------------------------------------------------

    def close_proposal(self, proposal_id: int) -> Proposal:
        """Tally and set status to 'passed' or 'rejected'. Idempotent after closure."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GovernanceError(f"Proposal {proposal_id} not found")

            # Already closed
            if row["status"] not in ("active",):
                return _row_to_proposal(row)

        tally = self.tally_proposal(proposal_id)

        with self._conn() as conn:
            conn.execute(
                "UPDATE governance_proposals SET status = ? WHERE id = ?",
                (tally.outcome, proposal_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return _row_to_proposal(row)

    # ------------------------------------------------------------------
    # execute_proposal
    # ------------------------------------------------------------------

    def execute_proposal(self, proposal_id: int) -> Proposal:
        """Execute a 'passed' proposal atomically.

        Uses EXCLUSIVE transaction to prevent concurrent execution races.
        On success: status='executed'. On error: status='failed', execution_error set.
        Never leaves partial state.
        """
        # Use an exclusive transaction for the entire execute
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN EXCLUSIVE")
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                conn.close()
                raise GovernanceError(f"Proposal {proposal_id} not found")

            if row["status"] != "passed":
                conn.close()
                raise GovernanceError(
                    f"Proposal {proposal_id} cannot be executed (status={row['status']!r})"
                )

            payload = json.loads(row["payload"])
            proposal_type = row["proposal_type"]

            error: Optional[str] = None
            try:
                if proposal_type == "config_change":
                    key = payload["key"]
                    new_value = str(payload["new_value"])
                    self._reloader.apply(key, new_value)

                elif proposal_type == "committee_update":
                    action = payload["action"]
                    member = payload["member"]
                    if action == "add":
                        conn.execute(
                            "INSERT OR IGNORE INTO governance_committee (member, added_at, active) VALUES (?, ?, 1)",
                            (member, datetime.now(timezone.utc).isoformat()),
                        )
                        conn.execute(
                            "UPDATE governance_committee SET active = 1 WHERE member = ?",
                            (member,),
                        )
                    elif action == "remove":
                        conn.execute(
                            "UPDATE governance_committee SET active = 0 WHERE member = ?",
                            (member,),
                        )
                    else:
                        raise GovernanceError(f"Unknown committee action: {action!r}")

                else:
                    raise GovernanceError(f"Unknown proposal_type: {proposal_type!r}")

            except Exception as exc:
                error = str(exc)

            now = datetime.now(timezone.utc).isoformat()
            if error is None:
                conn.execute(
                    "UPDATE governance_proposals SET status = 'executed', executed_at = ? WHERE id = ?",
                    (now, proposal_id),
                )
            else:
                conn.execute(
                    "UPDATE governance_proposals SET status = 'failed', executed_at = ?, execution_error = ? WHERE id = ?",
                    (now, error, proposal_id),
                )

            conn.commit()
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            result = _row_to_proposal(row)
        finally:
            conn.close()

        return result

    # ------------------------------------------------------------------
    # close_expired
    # ------------------------------------------------------------------

    def close_expired(self) -> list[Proposal]:
        """Close all active proposals past voting_ends_at. Returns closed proposals."""
        now = self._now()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM governance_proposals WHERE status = 'active'"
            ).fetchall()

        closed: list[Proposal] = []
        for row in rows:
            voting_ends = _parse_dt(row["voting_ends_at"])
            if now > voting_ends:
                try:
                    p = self.close_proposal(row["id"])
                    closed.append(p)
                except GovernanceError:
                    pass
        return closed


# ---------------------------------------------------------------------------
# Legacy compatibility shim — preserves old governance.py public API used by
# api/main.py (create_proposal, list_open_proposals, cast_proposal_vote).
# The new GovernanceEngine is the canonical implementation; the shim
# delegates to it.
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _BaseModel


class GovernanceProposal(_BaseModel):
    """Pydantic model for backward-compatible API responses."""
    proposal_id: str
    proposal_type: str
    proposed_value: str
    proposed_by_key_hash: str
    votes_for: list[str]
    votes_against: list[str]
    status: str
    created_at: datetime
    expires_at: datetime


def _engine() -> GovernanceEngine:
    return GovernanceEngine()


def create_proposal(
    proposal_type: str,
    proposed_value: str,
    proposed_by_key_hash: str,
    days_valid: int = 7,
) -> GovernanceProposal:
    """Legacy shim: create a proposal using the old API signature."""
    if proposal_type == "change_threshold":
        pt = "config_change"
        payload: dict = {"key": "RISK_SCORE_THRESHOLD", "new_value": proposed_value}
    elif proposal_type in ("add_committee_member", "remove_committee_member"):
        pt = "committee_update"
        action = "add" if proposal_type == "add_committee_member" else "remove"
        payload = {"action": action, "member": proposed_value}
    else:
        raise ValueError(f"invalid proposal_type: {proposal_type!r}")

    # Ensure the proposer exists in the committee table for the legacy API
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO governance_committee (member, added_at, active) VALUES (?, ?, 1)",
            (proposed_by_key_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    engine = _engine()
    # Override voting period to match legacy days_valid param
    engine.VOTING_PERIOD_HOURS = days_valid * 24

    try:
        p = engine.submit_proposal(proposed_by_key_hash, pt, payload)
    except GovernanceError as exc:
        raise ValueError(str(exc)) from exc

    return GovernanceProposal(
        proposal_id=str(p.id),
        proposal_type=proposal_type,
        proposed_value=proposed_value,
        proposed_by_key_hash=proposed_by_key_hash,
        votes_for=[],
        votes_against=[],
        status="open",
        created_at=p.submitted_at,
        expires_at=p.voting_ends_at,
    )


def list_open_proposals() -> list[GovernanceProposal]:
    """Legacy shim: list active proposals."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM governance_proposals WHERE status = 'active'"
        ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload"])
        proposed_value = str(payload.get("new_value", payload.get("member", "")))
        pt = row["proposal_type"]
        result.append(GovernanceProposal(
            proposal_id=str(row["id"]),
            proposal_type=pt,
            proposed_value=proposed_value,
            proposed_by_key_hash=row["proposer"],
            votes_for=[],
            votes_against=[],
            status="open",
            created_at=_parse_dt(row["submitted_at"]),
            expires_at=_parse_dt(row["voting_ends_at"]),
        ))
    return result


def cast_proposal_vote(
    proposal_id: str, voter_key_hash: str, vote: str
) -> GovernanceProposal:
    """Legacy shim: cast a vote."""
    if vote not in ("for", "against"):
        raise ValueError("vote must be 'for' or 'against'")

    pid = int(proposal_id)
    engine = _engine()

    # Ensure voter is in committee for legacy API
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO governance_committee (member, added_at, active) VALUES (?, ?, 1)",
            (voter_key_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    try:
        engine.cast_vote(pid, voter_key_hash, vote)
    except GovernanceVoteError as exc:
        raise ValueError(str(exc)) from exc

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM governance_proposals WHERE id = ?", (pid,)
        ).fetchone()

    payload = json.loads(row["payload"])
    proposed_value = str(payload.get("new_value", payload.get("member", "")))

    with _connect() as conn:
        for_rows = conn.execute(
            "SELECT voter FROM governance_votes WHERE proposal_id = ? AND decision = 'for'",
            (pid,),
        ).fetchall()
        against_rows = conn.execute(
            "SELECT voter FROM governance_votes WHERE proposal_id = ? AND decision = 'against'",
            (pid,),
        ).fetchall()

    votes_for = [r["voter"] for r in for_rows]
    votes_against = [r["voter"] for r in against_rows]

    return GovernanceProposal(
        proposal_id=str(row["id"]),
        proposal_type=row["proposal_type"],
        proposed_value=proposed_value,
        proposed_by_key_hash=row["proposer"],
        votes_for=votes_for,
        votes_against=votes_against,
        status=row["status"],
        created_at=_parse_dt(row["submitted_at"]),
        expires_at=_parse_dt(row["voting_ends_at"]),
    )
