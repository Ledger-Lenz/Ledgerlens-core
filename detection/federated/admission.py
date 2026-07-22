"""Participant admission control for the federated aggregation server.

Root cause this addresses: `FederatedAggregationServer.register_participant`
previously accepted *any* participant_id with a freshly-generated Ed25519 key
and no authorization step — any actor able to reach the registration endpoint
could mint an unlimited number of participant identities (Sybil), and once
registered, the server trusted a fully self-reported ``n_samples`` value with
no cap, which directly and proportionally controls FedAvg aggregation weight.

This module decouples registration from open self-service: a participant_id
must be explicitly *admitted* by an operator — out-of-band, via
:func:`admit_participant` (exposed through ``cli.py federated admit`` and the
admin-gated ``POST /v1/admin/federated/admit-participant`` endpoint) — before
:meth:`FederatedAggregationServer.register_participant` will accept a key for
it. Admission also assigns a ``max_n_samples`` ceiling: the largest sample
count the server will ever credit that participant for, regardless of what
it claims in a signed update payload (see ``server.py``'s
``_effective_n_samples``). This is the operator's out-of-band judgement call
(e.g. based on the institution's known transaction volume) standing in for a
verifiable dataset-size attestation -- see docs/federated_learning.md's
"Participant Admission & Weight Bounding" section for the full threat model
and why this was chosen over a ZK range proof or TEE attestation.

Storage mirrors ``detection.federated.audit``'s self-contained SQLite
pattern: a dedicated table, a `db_path` override for tests, module-level
functions rather than a class (this store has no in-memory state to manage
between calls, unlike the server itself).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generator

from config.settings import settings

_DEFAULT_DB_PATH = settings.db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS federated_admitted_participants (
    participant_id  TEXT PRIMARY KEY,
    max_n_samples   INTEGER NOT NULL,
    admitted_at     TEXT NOT NULL,
    admitted_by     TEXT NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    revoked_at      TEXT
);
"""


@contextmanager
def _connect(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class AdmissionRecord:
    participant_id: str
    max_n_samples: int
    admitted_at: str
    admitted_by: str
    revoked: bool


class AdmissionError(PermissionError):
    """Raised when an unadmitted or revoked participant_id attempts to register."""


def admit_participant(
    participant_id: str,
    max_n_samples: int,
    admitted_by: str,
    db_path: str | None = None,
) -> AdmissionRecord:
    """Authorize `participant_id` to register, capped at `max_n_samples`.

    Idempotent-by-replacement: re-admitting an existing (including
    previously-revoked) participant_id updates its ceiling and clears any
    revocation, so an operator can adjust a ceiling without a separate
    "un-revoke" call. `admitted_by` is a free-text operator identifier
    (e.g. an admin API key's namespace or an operator's own name) recorded
    for audit purposes -- it is not itself an authentication mechanism.
    """
    if max_n_samples <= 0:
        raise ValueError(f"max_n_samples must be positive, got {max_n_samples}")
    if not participant_id:
        raise ValueError("participant_id must not be empty")

    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO federated_admitted_participants
                (participant_id, max_n_samples, admitted_at, admitted_by, revoked, revoked_at)
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT(participant_id) DO UPDATE SET
                max_n_samples = excluded.max_n_samples,
                admitted_at = excluded.admitted_at,
                admitted_by = excluded.admitted_by,
                revoked = 0,
                revoked_at = NULL
            """,
            (participant_id, int(max_n_samples), now, admitted_by),
        )
        conn.commit()
    return AdmissionRecord(
        participant_id=participant_id,
        max_n_samples=int(max_n_samples),
        admitted_at=now,
        admitted_by=admitted_by,
        revoked=False,
    )


def revoke_admission(participant_id: str, db_path: str | None = None) -> bool:
    """Revoke a previously-admitted participant. Returns False if not found.

    Revocation does not retroactively invalidate updates already aggregated
    in past rounds (the audit log is append-only and immutable by design);
    it only prevents *new* registrations and, for an already-registered
    participant, must be paired with removing/rotating its key server-side
    if immediate exclusion from in-flight rounds is required (out of scope
    here -- this module governs admission, not live session revocation).
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE federated_admitted_participants SET revoked = 1, revoked_at = ? "
            "WHERE participant_id = ?",
            (datetime.now(timezone.utc).isoformat(), participant_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_admission(participant_id: str, db_path: str | None = None) -> AdmissionRecord | None:
    """Return the admission record for `participant_id`, or None if never admitted."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT participant_id, max_n_samples, admitted_at, admitted_by, revoked "
            "FROM federated_admitted_participants WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
    if row is None:
        return None
    return AdmissionRecord(
        participant_id=row["participant_id"],
        max_n_samples=row["max_n_samples"],
        admitted_at=row["admitted_at"],
        admitted_by=row["admitted_by"],
        revoked=bool(row["revoked"]),
    )


def is_admitted(participant_id: str, db_path: str | None = None) -> bool:
    """Return True iff `participant_id` has a current (non-revoked) admission."""
    record = get_admission(participant_id, db_path)
    return record is not None and not record.revoked


def list_admissions(db_path: str | None = None) -> list[AdmissionRecord]:
    """Return all admission records, most recently admitted first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT participant_id, max_n_samples, admitted_at, admitted_by, revoked "
            "FROM federated_admitted_participants ORDER BY admitted_at DESC"
        ).fetchall()
    return [
        AdmissionRecord(
            participant_id=r["participant_id"],
            max_n_samples=r["max_n_samples"],
            admitted_at=r["admitted_at"],
            admitted_by=r["admitted_by"],
            revoked=bool(r["revoked"]),
        )
        for r in rows
    ]
