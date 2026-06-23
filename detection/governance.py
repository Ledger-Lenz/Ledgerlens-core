from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import List

from pydantic import BaseModel

from detection.storage import init_db, _connect


class GovernanceProposal(BaseModel):
    proposal_id: str
    proposal_type: str
    proposed_value: str
    proposed_by_key_hash: str
    votes_for: List[str]
    votes_against: List[str]
    status: str
    created_at: datetime
    expires_at: datetime


def create_proposal(proposal_type: str, proposed_value: str, proposed_by_key_hash: str, days_valid: int = 7) -> GovernanceProposal:
    if proposal_type not in ("change_threshold", "add_committee_member", "remove_committee_member"):
        raise ValueError("invalid proposal_type")
    init_db()
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days_valid)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO governance_proposals (proposal_id, proposal_type, proposed_value, proposed_by_key_hash, votes_for_json, votes_against_json, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, proposal_type, proposed_value, proposed_by_key_hash, json.dumps([]), json.dumps([]), "open", now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    return GovernanceProposal(
        proposal_id=pid,
        proposal_type=proposal_type,
        proposed_value=proposed_value,
        proposed_by_key_hash=proposed_by_key_hash,
        votes_for=[],
        votes_against=[],
        status="open",
        created_at=now,
        expires_at=expires,
    )


def list_open_proposals() -> List[GovernanceProposal]:
    init_db()
    res: List[GovernanceProposal] = []
    with _connect() as conn:
        rows = conn.execute("SELECT proposal_id, proposal_type, proposed_value, proposed_by_key_hash, votes_for_json, votes_against_json, status, created_at, expires_at FROM governance_proposals WHERE status = 'open'").fetchall()
        for r in rows:
            res.append(
                GovernanceProposal(
                    proposal_id=r[0],
                    proposal_type=r[1],
                    proposed_value=r[2],
                    proposed_by_key_hash=r[3],
                    votes_for=json.loads(r[4]),
                    votes_against=json.loads(r[5]),
                    status=r[6],
                    created_at=datetime.fromisoformat(r[7]),
                    expires_at=datetime.fromisoformat(r[8]),
                )
            )
    return res


def cast_proposal_vote(proposal_id: str, voter_key_hash: str, vote: str) -> GovernanceProposal:
    if vote not in ("for", "against"):
        raise ValueError("vote must be 'for' or 'against'")
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT id, proposal_id, proposal_type, proposed_value, votes_for_json, votes_against_json, status, created_at, expires_at FROM governance_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError("proposal not found")
        row_id = row[0]
        status = row[6]
        if status != "open":
            raise ValueError("proposal not open")
        votes_for = json.loads(row[4])
        votes_against = json.loads(row[5])
        if voter_key_hash in votes_for or voter_key_hash in votes_against:
            raise ValueError("voter has already voted")
        if vote == "for":
            votes_for.append(voter_key_hash)
        else:
            votes_against.append(voter_key_hash)
        conn.execute("UPDATE governance_proposals SET votes_for_json = ?, votes_against_json = ? WHERE id = ?", (json.dumps(votes_for), json.dumps(votes_against), row_id))
        conn.commit()

        # Tally and decide
        total = len(votes_for) + len(votes_against)
        # For change_threshold require 3/4 supermajority
        if row[2] == "change_threshold":
            # need 3/4 supermajority when quorum reached: require at least 1 voter? we'll evaluate immediately
            if total > 0 and len(votes_for) * 4 >= 3 * total:
                # pass
                conn.execute("UPDATE governance_proposals SET status = ? WHERE id = ?", ("passed", row_id))
                conn.commit()
                # Apply change to runtime_config
                try:
                    conn.execute("INSERT OR REPLACE INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)", ("risk_score_threshold", row[3], datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                except Exception:
                    pass
                return GovernanceProposal(proposal_id=row[1], proposal_type=row[2], proposed_value=row[3], proposed_by_key_hash=row[3], votes_for=votes_for, votes_against=votes_against, status="passed", created_at=datetime.fromisoformat(row[7]), expires_at=datetime.fromisoformat(row[8]))
        else:
            # simple majority for add/remove committee
            if total > 0 and len(votes_for) > len(votes_against):
                conn.execute("UPDATE governance_proposals SET status = ? WHERE id = ?", ("passed", row_id))
                conn.commit()
                return GovernanceProposal(proposal_id=row[1], proposal_type=row[2], proposed_value=row[3], proposed_by_key_hash=row[3], votes_for=votes_for, votes_against=votes_against, status="passed", created_at=datetime.fromisoformat(row[7]), expires_at=datetime.fromisoformat(row[8]))

        # check expiry
        expires_at = datetime.fromisoformat(row[8])
        if datetime.now(timezone.utc) > expires_at:
            conn.execute("UPDATE governance_proposals SET status = ? WHERE id = ?", ("expired", row_id))
            conn.commit()
            return GovernanceProposal(proposal_id=row[1], proposal_type=row[2], proposed_value=row[3], proposed_by_key_hash=row[3], votes_for=votes_for, votes_against=votes_against, status="expired", created_at=datetime.fromisoformat(row[7]), expires_at=expires_at)

        return GovernanceProposal(proposal_id=row[1], proposal_type=row[2], proposed_value=row[3], proposed_by_key_hash=row[3], votes_for=votes_for, votes_against=votes_against, status="open", created_at=datetime.fromisoformat(row[7]), expires_at=datetime.fromisoformat(row[8]))