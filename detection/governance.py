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

import hashlib
import json
import math
import os
import re
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


class GovernanceApprovalError(GovernanceError):
    """Raised when a change lacks the sign-offs/evidence its approval policy requires."""


# ---------------------------------------------------------------------------
# Approval policy (see docs/governance_protocol.md, "Change approval workflow")
#
# A passed committee vote is necessary but not sufficient to execute a
# significant change: each proposal type also needs named sign-offs from
# distinct committee members in specific roles, and the evidence listed
# below must be attached to those sign-offs.  The proposer can never sign
# off their own change, and one person cannot fill two roles.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalPolicy:
    roles: frozenset[str]
    evidence: frozenset[str]


APPROVAL_POLICIES: dict[str, ApprovalPolicy] = {
    # Detection-policy change (thresholds, confidence floors, ...).
    "config_change": ApprovalPolicy(
        roles=frozenset({"risk_owner", "compliance_officer"}),
        evidence=frozenset({"impact_analysis"}),
    ),
    # Promotion of a new model version to live inference.
    "model_promotion": ApprovalPolicy(
        roles=frozenset({"model_owner", "independent_validator", "compliance_officer"}),
        evidence=frozenset({"backtest_report", "robustness_report", "red_team_report"}),
    ),
    # Committee membership is governed by the committee vote alone.
    "committee_update": ApprovalPolicy(roles=frozenset(), evidence=frozenset()),
}

_MODEL_NAME_RE = re.compile(r"^[a-z0-9_]+$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    id: Optional[int]
    proposal_type: Literal["config_change", "committee_update", "model_promotion"]
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
class SignOff:
    proposal_id: int
    role: str
    signer: str
    evidence: dict
    signed_at: datetime


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

        Does NOT write `runtime_config` or signal cross-process propagation
        itself -- when called via `GovernanceEngine.execute_proposal` (the
        only real caller), that method performs the `runtime_config` write
        on its own already-open `BEGIN EXCLUSIVE` connection, atomically with
        the `status='executed'` transition, and calls `bump_config_version()`
        after committing. A second, independent connection opened here would
        deadlock against that exclusive lock (verified: it silently failed
        every time, swallowed by an overly broad `except Exception: pass`,
        which is exactly why "the mechanism was wired to a function nothing
        calls" -- the write never even landed). A caller using
        `SettingsReloader` standalone, outside `execute_proposal`, gets only
        the in-process live-apply and `.env` write below; it will not
        propagate to `runtime_config` or other processes.
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

    @staticmethod
    def _ensure_approval_tables(conn) -> None:
        # Separate execute() calls, not executescript(): the latter commits
        # first, which would drop execute_proposal's BEGIN EXCLUSIVE lock.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_signoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                signer TEXT NOT NULL,
                evidence TEXT NOT NULL,
                signed_at TIMESTAMP NOT NULL,
                UNIQUE(proposal_id, role),
                UNIQUE(proposal_id, signer)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_approval_records (
                proposal_id INTEGER PRIMARY KEY,
                approval_chain TEXT NOT NULL,
                chain_sha256 TEXT NOT NULL,
                recorded_at TIMESTAMP NOT NULL
            )
        """)

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
        if proposal_type not in APPROVAL_POLICIES:
            raise GovernanceError(f"Invalid proposal_type: {proposal_type!r}")

        if proposal_type == "model_promotion":
            name = str(payload.get("model_name", ""))
            if not _MODEL_NAME_RE.match(name) or not payload.get("version"):
                raise GovernanceError("model_promotion requires a valid model_name and version")

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

    def record_signoff(self, proposal_id: int, signer: str, role: str, evidence: dict | None = None) -> SignOff:
        """Record a named sign-off in ``role`` for a proposal, with its evidence.

        ``evidence`` maps evidence kinds (e.g. ``"red_team_report"``) to a
        reference such as a report path, URL or artifact hash.
        """
        evidence = {k: v for k, v in (evidence or {}).items() if v}
        with self._conn() as conn:
            self._ensure_approval_tables(conn)
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GovernanceError(f"Proposal {proposal_id} not found")
            if row["status"] not in ("active", "passed"):
                raise GovernanceApprovalError(
                    f"Proposal {proposal_id} is {row['status']!r}; sign-offs are closed"
                )
            policy = APPROVAL_POLICIES[row["proposal_type"]]
            if role not in policy.roles:
                raise GovernanceApprovalError(
                    f"Role {role!r} is not part of the {row['proposal_type']} approval policy"
                )
            if not self._is_committee_member(conn, signer):
                raise GovernanceApprovalError(f"Signer {signer!r} is not an active committee member")
            if signer == row["proposer"]:
                raise GovernanceApprovalError("The proposer cannot sign off their own change")

            now = self._now()
            try:
                conn.execute(
                    """INSERT INTO governance_signoffs (proposal_id, role, signer, evidence, signed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (proposal_id, role, signer, json.dumps(evidence, sort_keys=True), now.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise GovernanceApprovalError(
                    f"Role {role!r} is already signed, or {signer!r} already signed another role"
                ) from exc
            conn.commit()
        return SignOff(proposal_id=proposal_id, role=role, signer=signer, evidence=evidence, signed_at=now)

    @staticmethod
    def _build_approval_chain(conn, row) -> dict:
        """Assemble the full approval chain for a proposal row on ``conn``."""
        policy = APPROVAL_POLICIES[row["proposal_type"]]
        votes = conn.execute(
            "SELECT voter, decision, cast_at FROM governance_votes WHERE proposal_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        signoffs = conn.execute(
            "SELECT role, signer, evidence, signed_at FROM governance_signoffs WHERE proposal_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        signoff_list = [
            {"role": r["role"], "signer": r["signer"], "evidence": json.loads(r["evidence"]), "signed_at": r["signed_at"]}
            for r in signoffs
        ]
        evidence_kinds = {k for so in signoff_list for k in so["evidence"]}
        return {
            "proposal_id": row["id"],
            "proposal_type": row["proposal_type"],
            "payload": json.loads(row["payload"]),
            "proposer": row["proposer"],
            "submitted_at": row["submitted_at"],
            "status": row["status"],
            "votes": [dict(v) for v in votes],
            "signoffs": signoff_list,
            "missing_roles": sorted(policy.roles - {so["role"] for so in signoff_list}),
            "missing_evidence": sorted(policy.evidence - evidence_kinds),
        }

    def approval_chain(self, proposal_id: int) -> dict:
        """Return the current approval chain (votes, sign-offs, gaps) for a proposal."""
        with self._conn() as conn:
            self._ensure_approval_tables(conn)
            row = conn.execute(
                "SELECT * FROM governance_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise GovernanceError(f"Proposal {proposal_id} not found")
            return self._build_approval_chain(conn, row)

    def recorded_approval(self, proposal_id: int) -> dict | None:
        """Return the immutable approval chain recorded when a proposal executed."""
        with self._conn() as conn:
            self._ensure_approval_tables(conn)
            row = conn.execute(
                "SELECT * FROM governance_approval_records WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "approval_chain": json.loads(row["approval_chain"]),
            "chain_sha256": row["chain_sha256"],
            "recorded_at": row["recorded_at"],
        }

    def live_change_approval(self, proposal_type: str, target: str) -> dict | None:
        """Trace a live setting or model to the approval chain that put it live.

        ``target`` is the setting key for ``config_change`` or the model name
        for ``model_promotion``.  Returns the recorded chain of the most
        recently executed proposal for that target, or ``None``.
        """
        field_name = {"config_change": "key", "model_promotion": "model_name"}[proposal_type]
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, payload FROM governance_proposals
                   WHERE proposal_type = ? AND status = 'executed' ORDER BY executed_at DESC, id DESC""",
                (proposal_type,),
            ).fetchall()
        for row in rows:
            if json.loads(row["payload"]).get(field_name) == target:
                return self.recorded_approval(row["id"])
        return None

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

            # Enforce the approval policy: a passed vote alone is not enough.
            self._ensure_approval_tables(conn)
            chain = self._build_approval_chain(conn, row)
            if chain["missing_roles"] or chain["missing_evidence"]:
                conn.close()
                raise GovernanceApprovalError(
                    f"Proposal {proposal_id} lacks required approvals: "
                    f"missing roles {chain['missing_roles']}, missing evidence {chain['missing_evidence']}"
                )

            error: Optional[str] = None
            try:
                if proposal_type == "config_change":
                    key = payload["key"]
                    new_value = str(payload["new_value"])
                    self._reloader.apply(key, new_value)

                    # Propagate to every process via `runtime_config`, on this
                    # same EXCLUSIVE connection/transaction -- atomic with the
                    # 'executed' transition below, and immune to the deadlock
                    # a second connection would hit against the lock this
                    # transaction already holds (see `SettingsReloader.apply`'s
                    # docstring). A write failure here fails the whole
                    # proposal (status='failed') rather than reaching
                    # 'executed' without actually propagating, per this
                    # issue's requirement that a governance-approved change
                    # must actually take effect, not just be recorded as if
                    # it had.
                    conn.execute(
                        "INSERT OR REPLACE INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)",
                        (key.lower(), new_value, datetime.now(timezone.utc).isoformat()),
                    )

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

                elif proposal_type == "model_promotion":
                    from detection.model_registry import list_model_versions, rollback_model

                    name, version = payload["model_name"], str(payload["version"])
                    if version not in list_model_versions(name, settings.model_dir):
                        raise GovernanceError(f"Model {name} version {version} not found in model registry")
                    rollback_model(name, version, settings.model_dir)

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
                chain["status"] = "executed"
                chain["executed_at"] = now
                chain_json = json.dumps(chain, sort_keys=True)
                conn.execute(
                    """INSERT INTO governance_approval_records
                       (proposal_id, approval_chain, chain_sha256, recorded_at) VALUES (?, ?, ?, ?)""",
                    (proposal_id, chain_json, hashlib.sha256(chain_json.encode("utf-8")).hexdigest(), now),
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

        # Signal every other process to re-poll `runtime_config` immediately
        # rather than waiting out their local TTL (see config/settings.py's
        # consistency-model docstring). Only after the commit above, so a
        # woken process's re-read is guaranteed to see the durable value.
        # Best-effort: Redis being unavailable must not affect the proposal
        # outcome already committed above.
        if result.status == "executed" and proposal_type == "config_change":
            try:
                from config.settings import bump_config_version
                bump_config_version()
            except Exception:
                pass

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
