"""Cryptographically signed federated-learning audit log.

Each federated round produces a signed audit record stored in the
`federated_audit_log` SQLite table.  Records can be verified offline
using the server's Ed25519 public key.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from config.settings import settings

_DEFAULT_DB_PATH = settings.db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS federated_audit_log (
    round_id            TEXT PRIMARY KEY,
    participants_json   TEXT NOT NULL,
    aggregated_update_norm REAL NOT NULL,
    dp_epsilon_consumed REAL NOT NULL,
    cumulative_epsilon  REAL NOT NULL,
    timestamp           TEXT NOT NULL,
    excluded_participants_json TEXT NOT NULL DEFAULT '[]',
    record_json         TEXT NOT NULL,
    signature           BLOB NOT NULL,
    dp_delta            REAL NOT NULL DEFAULT 0.0,
    noise_multiplier    REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS federated_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_MIGRATIONS = [
    ("dp_delta", "REAL NOT NULL DEFAULT 0.0"),
    ("noise_multiplier", "REAL NOT NULL DEFAULT 0.0"),
]


@contextmanager
def _connect(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        for col, definition in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE federated_audit_log ADD COLUMN {col} {definition}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        yield conn
    finally:
        conn.close()


def hash_participant_id(participant_id: str) -> str:
    """Return SHA-256 hex digest of a participant identifier."""
    return hashlib.sha256(participant_id.encode()).hexdigest()


def build_record(
    round_id: str,
    participant_ids: list[str],
    aggregated_update_norm: float,
    dp_epsilon_consumed: float,
    cumulative_epsilon: float,
    excluded_participant_ids: list[str] | None = None,
    dp_delta: float = 0.0,
    noise_multiplier: float = 0.0,
) -> dict:
    """Build an unsigned audit record dict (all participant IDs are hashed)."""
    return {
        "round_id": round_id,
        "participants": [hash_participant_id(p) for p in participant_ids],
        "excluded_participants": [hash_participant_id(p) for p in (excluded_participant_ids or [])],
        "aggregated_update_norm": aggregated_update_norm,
        "dp_epsilon_consumed": dp_epsilon_consumed,
        "cumulative_epsilon": cumulative_epsilon,
        "dp_delta": dp_delta,
        "noise_multiplier": noise_multiplier,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def sign_record(record: dict, private_key: Ed25519PrivateKey) -> bytes:
    """Return an Ed25519 signature over the canonical JSON encoding of `record`."""
    payload = json.dumps(record, sort_keys=True).encode()
    return private_key.sign(payload)


def verify_record(record: dict, signature: bytes, public_key: Ed25519PublicKey) -> bool:
    """Return True if `signature` is valid for `record` under `public_key`."""
    from cryptography.exceptions import InvalidSignature

    payload = json.dumps(record, sort_keys=True).encode()
    try:
        public_key.verify(signature, payload)
        return True
    except InvalidSignature:
        return False


def save_audit_record(
    record: dict,
    signature: bytes,
    db_path: str | None = None,
) -> None:
    """Persist a signed audit record to the database."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO federated_audit_log
                (round_id, participants_json, aggregated_update_norm,
                 dp_epsilon_consumed, cumulative_epsilon, timestamp,
                 excluded_participants_json, record_json, signature,
                 dp_delta, noise_multiplier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["round_id"],
                json.dumps(record["participants"]),
                record["aggregated_update_norm"],
                record["dp_epsilon_consumed"],
                record["cumulative_epsilon"],
                record["timestamp"],
                json.dumps(record.get("excluded_participants", [])),
                json.dumps(record, sort_keys=True),
                signature,
                record.get("dp_delta", 0.0),
                record.get("noise_multiplier", 0.0),
            ),
        )
        conn.commit()


def get_audit_records(
    limit: int = 100,
    db_path: str | None = None,
) -> list[dict]:
    """Return the most recent `limit` audit records (newest first)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT record_json, signature FROM federated_audit_log
            ORDER BY timestamp DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        rec = json.loads(row["record_json"])
        rec["_signature_hex"] = row["signature"].hex()
        results.append(rec)
    return results


def get_cumulative_epsilon(db_path: str | None = None) -> float:
    """Return the latest cumulative privacy-budget consumption."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT cumulative_epsilon FROM federated_audit_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return float(row["cumulative_epsilon"]) if row else 0.0


def get_round_count(db_path: str | None = None) -> int:
    """Return the total number of completed federated rounds persisted in the audit log."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM federated_audit_log").fetchone()
    return int(row["n"]) if row else 0
