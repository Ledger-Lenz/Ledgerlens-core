"""Dual-approval, expiry, and audit trail for manual score overrides (Issue #995).

Wallet allowlist/denylist overrides (:mod:`detection.wallet_override_store`)
and alert suppressions (:mod:`detection.suppressions`) both bypass detection
results. To keep a single compromised or malicious admin from applying one
alone, every override/suppression must name at least ``MIN_APPROVERS``
distinct approvers, none of whom is the requester, and must expire. See
``docs/override_policy.md`` for the policy.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("ledgerlens.override_approval")

MIN_APPROVERS = 2
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 180

_CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS override_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    wallet TEXT NOT NULL,
    action TEXT NOT NULL,
    requester TEXT NOT NULL,
    approvers TEXT NOT NULL,
    justification TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    created_at TEXT NOT NULL
)
"""


class ApprovalError(ValueError):
    """Raised when an override lacks the required approvals, justification, or a valid expiry."""


def validate_approval(requester: str, approvers: list[str] | None, justification: str) -> list[str]:
    """Return the normalized approver list, or raise :class:`ApprovalError`.

    Approvers are compared case-insensitively; the requester never counts as
    an approver of their own request.
    """
    requester = (requester or "").strip()
    if not requester:
        raise ApprovalError("A requester is required")
    if not (justification or "").strip():
        raise ApprovalError("A justification is required")

    distinct: dict[str, str] = {}
    for name in approvers or []:
        name = (name or "").strip()
        if name and name.lower() != requester.lower():
            distinct.setdefault(name.lower(), name)
    if len(distinct) < MIN_APPROVERS:
        raise ApprovalError(
            f"At least {MIN_APPROVERS} distinct approvers other than the requester are required "
            f"(got {len(distinct)})"
        )
    return sorted(distinct.values())


def resolve_expiry(expires_at: str | None, now: datetime | None = None) -> str:
    """Return an ISO-8601 UTC expiry, defaulting to ``DEFAULT_TTL_DAYS`` and capped at ``MAX_TTL_DAYS``."""
    now = now or datetime.now(timezone.utc)
    if expires_at is None:
        return (now + timedelta(days=DEFAULT_TTL_DAYS)).isoformat()
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ApprovalError(f"Invalid expires_at: {expires_at!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= now:
        raise ApprovalError("expires_at must be in the future")
    if parsed > now + timedelta(days=MAX_TTL_DAYS):
        raise ApprovalError(f"expires_at may be at most {MAX_TTL_DAYS} days out")
    return parsed.astimezone(timezone.utc).isoformat()


def default_expiry_for_legacy_rows() -> str:
    """Expiry assigned to pre-existing rows on upgrade so they get re-reviewed."""
    return (datetime.now(timezone.utc) + timedelta(days=DEFAULT_TTL_DAYS)).isoformat()


def ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_AUDIT_TABLE)


def record_audit(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    wallet: str,
    action: str,
    requester: str,
    approvers: list[str],
    justification: str,
    before: dict | None,
    after: dict | None,
) -> None:
    """Append one audit record. Callers commit it in the same transaction as the change."""
    ensure_audit_table(conn)
    conn.execute(
        "INSERT INTO override_audit_log (target_type, target_id, wallet, action, requester, approvers, "
        "justification, before_state, after_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_type,
            str(target_id),
            wallet,
            action,
            requester,
            json.dumps(approvers),
            justification,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    logger.info(
        "Override audit: target_type=%s target_id=%s wallet=%s action=%s requester=%s approvers=%s",
        target_type, target_id, wallet, action, requester, approvers,
    )


def list_audit_records(
    conn: sqlite3.Connection,
    target_type: str | None = None,
    wallet: str | None = None,
) -> list[dict]:
    """Return audit records oldest first, optionally filtered by target type and/or wallet."""
    ensure_audit_table(conn)
    clauses, params = [], []
    if target_type:
        clauses.append("target_type = ?")
        params.append(target_type)
    if wallet:
        clauses.append("wallet = ?")
        params.append(wallet)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM override_audit_log{where} ORDER BY id", params).fetchall()
    records = []
    for r in rows:
        rec = dict(r)
        rec["approvers"] = json.loads(rec["approvers"])
        for key in ("before_state", "after_state"):
            rec[key] = json.loads(rec[key]) if rec[key] else None
        records.append(rec)
    return records
