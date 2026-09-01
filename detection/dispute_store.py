from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import List
from urllib.parse import urlparse

from pydantic import BaseModel

from config.settings import settings
from detection.chain_submission_queue import enqueue_override_submission
from detection.storage import init_db, _connect


class ScoreDispute(BaseModel):
    dispute_id: str
    wallet: str
    asset_pair: str
    disputed_score: int
    soroban_tx_hash: str
    evidence_url: str | None
    submitted_at: datetime
    status: str
    committee_votes: List[dict]
    resolved_at: datetime | None = None
    resolution: str | None = None


# Helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_evidence_url(evidence_url: str) -> None:
    parsed = urlparse(evidence_url)
    if parsed.scheme.lower() != "https":
        raise ValueError("evidence_url must use https scheme")
    # Reject private IPs by hostname starting with common private ranges
    host = parsed.hostname or ""
    if host.startswith("10.") or host.startswith("127.") or host.startswith("192.168."):
        raise ValueError("evidence_url must not point to private IP ranges")
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                second = int(parts[1])
                if 16 <= second <= 31:
                    raise ValueError("evidence_url must not point to private IP ranges")
            except ValueError:
                pass


def _update_override_status(override_id: int, status: str, tx_hash: str | None = None) -> None:
    """Atomically update a single score_override row status and optional tx_hash."""
    conn = sqlite3.connect(settings.db_path)
    try:
        if tx_hash:
            conn.execute(
                "UPDATE score_overrides SET tx_hash = ?, status = ? WHERE id = ?",
                (tx_hash, status, override_id),
            )
        else:
            conn.execute(
                "UPDATE score_overrides SET status = ? WHERE id = ?",
                (status, override_id),
            )
        conn.commit()
    finally:
        conn.close()


def apply_submission_result(job: dict, tx_hash: str | None) -> None:
    """Mirror a confirmed on-chain write back onto its `score_overrides` row.

    Passed to `chain_submission_queue.process_once`/`run_worker` as the
    `on_submitted` callback, so the queue stays agnostic about the tables its
    jobs originated from. Only ever called after the chain has accepted the
    write, so `'submitted'` here means submitted.
    """
    override_id = job.get("override_id")
    if override_id is None:
        return
    _update_override_status(override_id, "submitted", tx_hash)


# Public API

def submit_dispute(wallet: str, asset_pair: str, evidence_url: str | None = None) -> ScoreDispute:
    """Create a dispute for the latest on-chain submission for wallet/asset_pair.

    Raises ValueError for missing submission or rate-limit violations.
    """
    init_db()
    if evidence_url is not None:
        _validate_evidence_url(evidence_url)

    with _connect() as conn:
        # Find latest on-chain submission for wallet/asset_pair
        row = conn.execute(
            "SELECT tx_hash, submitted_at FROM on_chain_submissions WHERE wallet = ? AND asset_pair = ? ORDER BY submitted_at DESC LIMIT 1",
            (wallet, asset_pair),
        ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("No on-chain submission found for wallet/asset_pair")
        tx_hash = row[0]

        # Rate-limiting: check last dispute for this wallet/asset_pair
        last = conn.execute(
            "SELECT submitted_at FROM score_disputes WHERE wallet = ? AND asset_pair = ? ORDER BY submitted_at DESC LIMIT 1",
            (wallet, asset_pair),
        ).fetchone()
        if last is not None:
            last_ts = datetime.fromisoformat(last[0])
            if datetime.now(timezone.utc) - last_ts < timedelta(days=7):
                raise ValueError("Rate limit: dispute already submitted within 7 days")

        # Get latest score from risk_scores
        score_row = conn.execute(
            "SELECT score FROM risk_scores WHERE wallet = ? AND asset_pair = ? ORDER BY timestamp DESC LIMIT 1",
            (wallet, asset_pair),
        ).fetchone()
        if score_row is None:
            disputed_score = 0
        else:
            disputed_score = int(score_row[0])

        dispute_id = str(uuid.uuid4())
        ts = _now_iso()
        votes_json = json.dumps([])

        conn.execute(
            "INSERT INTO score_disputes (dispute_id, wallet, asset_pair, disputed_score, soroban_tx_hash, evidence_url, submitted_at, status, committee_votes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, wallet, asset_pair, disputed_score, tx_hash, evidence_url, ts, "pending", votes_json),
        )
        conn.commit()

    return ScoreDispute(
        dispute_id=dispute_id,
        wallet=wallet,
        asset_pair=asset_pair,
        disputed_score=disputed_score,
        soroban_tx_hash=tx_hash,
        evidence_url=evidence_url,
        submitted_at=datetime.fromisoformat(ts),
        status="pending",
        committee_votes=[],
    )


def _load_dispute_row(conn: sqlite3.Connection, dispute_id: str) -> tuple | None:
    return conn.execute(
        "SELECT id, dispute_id, wallet, asset_pair, disputed_score, soroban_tx_hash, evidence_url, submitted_at, status, committee_votes_json, resolved_at, resolution FROM score_disputes WHERE dispute_id = ?",
        (dispute_id,),
    ).fetchone()


def _write_dispute_row(conn: sqlite3.Connection, row_id: int, status: str, votes: list, resolved_at: datetime | None = None, resolution: str | None = None) -> None:
    conn.execute(
        "UPDATE score_disputes SET status = ?, committee_votes_json = ?, resolved_at = ?, resolution = ? WHERE id = ?",
        (status, json.dumps(votes), resolved_at.isoformat() if resolved_at else None, resolution, row_id),
    )
    conn.commit()


def cast_vote(dispute_id: str, voter_key_hash: str, vote: str) -> ScoreDispute:
    if vote not in ("approve", "reject"):
        raise ValueError("vote must be 'approve' or 'reject'")
    if len(voter_key_hash) != 64:
        raise ValueError("voter_key_hash must be 64 hex chars")

    init_db()
    with _connect() as conn:
        row = _load_dispute_row(conn, dispute_id)
        if row is None:
            raise ValueError("Dispute not found")
        row_id = row[0]
        status = row[8]
        votes_json = row[9]
        if status in ("approved", "rejected", "resolved"):
            raise ValueError("Dispute already resolved and immutable")
        votes = json.loads(votes_json)
        # Check duplicate voter
        for v in votes:
            if v.get("voter_key_hash") == voter_key_hash:
                raise ValueError("Voter has already voted")
        votetime = datetime.now(timezone.utc).isoformat()
        votes.append({"voter_key_hash": voter_key_hash, "vote": vote, "voted_at": votetime})

        # Persist vote
        conn.execute(
            "UPDATE score_disputes SET committee_votes_json = ? WHERE id = ?",
            (json.dumps(votes), row_id),
        )
        conn.commit()

        # Check quorum
        quorum = getattr(settings, "COMMITTEE_QUORUM", 3)
        if len(votes) >= quorum:
            approves = sum(1 for v in votes if v.get("vote") == "approve")
            rejects = sum(1 for v in votes if v.get("vote") == "reject")
            total = approves + rejects
            # supermajority 2/3
            if approves * 3 >= 2 * total:
                # Approve: remove score, record override, publish zero score
                resolved_at = datetime.now(timezone.utc)
                _write_dispute_row(conn, row_id, "approved", votes, resolved_at, "score_removed")
                wallet = row[2]
                asset_pair = row[3]

                # Record the override and the durable obligation to publish it
                # *before* deleting the local row, then commit all three
                # together. Ordering matters: the previous code deleted the
                # score first and handed publication to a daemon thread, so a
                # crash — or simply the process exiting — in the window that
                # followed left the wallet locally cleared but still carrying
                # its old score on the public registry, with nothing recorded
                # anywhere that a write was still owed.
                ts = _now_iso()
                conn.execute(
                    "INSERT INTO score_overrides (wallet, asset_pair, dispute_id, tx_hash, status, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (wallet, asset_pair, dispute_id, None, "pending", ts),
                )
                override_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                enqueue_override_submission(
                    dispute_id=dispute_id,
                    override_id=override_id,
                    wallet=wallet,
                    asset_pair=asset_pair,
                    conn=conn,
                )
                conn.execute(
                    "DELETE FROM risk_scores WHERE wallet = ? AND asset_pair = ?",
                    (wallet, asset_pair),
                )
                conn.commit()

                return ScoreDispute(
                    dispute_id=row[1],
                    wallet=row[2],
                    asset_pair=row[3],
                    disputed_score=row[4],
                    soroban_tx_hash=row[5],
                    evidence_url=row[6],
                    submitted_at=datetime.fromisoformat(row[7]),
                    status="approved",
                    committee_votes=votes,
                    resolved_at=resolved_at,
                    resolution="score_removed",
                )
            if rejects * 3 >= 2 * total:
                resolved_at = datetime.now(timezone.utc)
                _write_dispute_row(conn, row_id, "rejected", votes, resolved_at, "upheld")
                return ScoreDispute(
                    dispute_id=row[1],
                    wallet=row[2],
                    asset_pair=row[3],
                    disputed_score=row[4],
                    soroban_tx_hash=row[5],
                    evidence_url=row[6],
                    submitted_at=datetime.fromisoformat(row[7]),
                    status="rejected",
                    committee_votes=votes,
                    resolved_at=resolved_at,
                    resolution="upheld",
                )

        # Not resolved yet; return pending
        return ScoreDispute(
            dispute_id=row[1],
            wallet=row[2],
            asset_pair=row[3],
            disputed_score=row[4],
            soroban_tx_hash=row[5],
            evidence_url=row[6],
            submitted_at=datetime.fromisoformat(row[7]),
            status="pending",
            committee_votes=votes,
        )


def get_resolved_disputes(
    since: datetime,
    db_path: str | None = None,
) -> list[ScoreDispute]:
    """Return disputes resolved at or after `since`."""
    cutoff = since.isoformat()
    path = db_path or settings.db_path
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        rows = conn.execute(
            "SELECT dispute_id, wallet, asset_pair, disputed_score, soroban_tx_hash, "
            "evidence_url, submitted_at, status, committee_votes_json, resolved_at, resolution "
            "FROM score_disputes WHERE resolved_at >= ? ORDER BY resolved_at",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [
        ScoreDispute(
            dispute_id=r[0],
            wallet=r[1],
            asset_pair=r[2],
            disputed_score=r[3],
            soroban_tx_hash=r[4],
            evidence_url=r[5],
            submitted_at=datetime.fromisoformat(r[6]),
            status=r[7],
            committee_votes=json.loads(r[8]),
            resolved_at=datetime.fromisoformat(r[9]) if r[9] else None,
            resolution=r[10],
        )
        for r in rows
    ]


def get_dispute(dispute_id: str) -> ScoreDispute | None:
    init_db()
    with _connect() as conn:
        row = _load_dispute_row(conn, dispute_id)
        if row is None:
            return None
        votes = json.loads(row[9])
        return ScoreDispute(
            dispute_id=row[1],
            wallet=row[2],
            asset_pair=row[3],
            disputed_score=row[4],
            soroban_tx_hash=row[5],
            evidence_url=row[6],
            submitted_at=datetime.fromisoformat(row[7]),
            status=row[8],
            committee_votes=votes,
            resolved_at=datetime.fromisoformat(row[10]) if row[10] else None,
            resolution=row[11],
        )
