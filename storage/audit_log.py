"""Immutable, append-only audit log with HMAC-SHA256 chain validation.

Each entry is linked to the previous via ``prev_hash``, forming a tamper-evident
chain. The ``entry_hash`` of every row is computed as::

    entry_hash = HMAC-SHA256(key=AUDIT_SECRET, msg=canonical_json(entry_without_hash))

where ``canonical_json`` produces a deterministic, sorted-key JSON encoding of
all columns *except* ``entry_hash``.

A ``genesis`` entry (prev_hash="genesis") is automatically inserted when the
table is first created.

Event types logged:
    - ``score_computed``       — a risk score was computed for a wallet
    - ``api_key_used``         — an API key was used to access the system
    - ``admin_config_changed`` — an admin changed a configuration value
    - ``suppression_rule_added``   — a suppression rule was added
    - ``suppression_rule_removed`` — a suppression rule was removed
    - ``audit_chain_verified`` — the full chain was verified (integrity self-check)
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ledgerlens.audit_log")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB_NAME = "audit_log.db"
GENESIS_PREV_HASH = "genesis"
AUDIT_SECRET_ENV_KEY = "LEDGERLENS_AUDIT_SECRET"
_DEFAULT_AUDIT_SECRET = "ledgerlens-audit-default-secret-do-not-use-in-prod"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_audit_secret() -> bytes:
    """Return the HMAC key from the environment (or a dev-only default)."""
    secret = os.getenv(AUDIT_SECRET_ENV_KEY, _DEFAULT_AUDIT_SECRET)
    return secret.encode("utf-8")


def _canonical_json(record: dict) -> bytes:
    """Deterministic, sorted-key JSON encoding."""
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_entry_hash(record: dict) -> str:
    """Compute ``entry_hash = HMAC-SHA256(key, canonical_json(record_without_hash))``."""
    entry = {k: v for k, v in record.items() if k != "entry_hash"}
    key = _get_audit_secret()
    return hmac.new(key, _canonical_json(entry), hashlib.sha256).hexdigest()


def _default_db_path() -> str:
    return str(Path(os.getcwd()) / DEFAULT_DB_NAME)


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------


def init_db(db_path: Optional[str] = None) -> None:
    """Create the ``audit_log`` table and insert the genesis entry if empty."""
    db_path = db_path or _default_db_path()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                event_type  TEXT    NOT NULL,
                actor       TEXT    NOT NULL,
                wallet      TEXT,
                score       INTEGER,
                prev_hash   TEXT    NOT NULL,
                entry_hash  TEXT    NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log (event_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log (timestamp)"
        )

        # Insert genesis row if the table is empty
        row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        if row[0] == 0:
            genesis = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "genesis",
                "actor": "system",
                "wallet": None,
                "score": None,
                "prev_hash": GENESIS_PREV_HASH,
            }
            genesis["entry_hash"] = _compute_entry_hash(genesis)
            conn.execute(
                """
                INSERT INTO audit_log (timestamp, event_type, actor, wallet, score, prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    genesis["timestamp"],
                    genesis["event_type"],
                    genesis["actor"],
                    genesis["wallet"],
                    genesis["score"],
                    genesis["prev_hash"],
                    genesis["entry_hash"],
                ),
            )
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Core append operation
# ---------------------------------------------------------------------------


def append_entry(
    event_type: str,
    actor: str,
    wallet: Optional[str] = None,
    score: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Append a single entry to the audit log and return it as a dict.

    The chain is formed by reading the ``entry_hash`` of the most recent row
    and using it as the new entry's ``prev_hash``.
    """
    db_path = db_path or _default_db_path()
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # Get the hash of the last entry in the chain
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = row[0] if row else GENESIS_PREV_HASH

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "actor": actor,
            "wallet": wallet,
            "score": score,
            "prev_hash": prev_hash,
        }
        entry["entry_hash"] = _compute_entry_hash(entry)

        conn.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, actor, wallet, score, prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["timestamp"],
                entry["event_type"],
                entry["actor"],
                entry["wallet"],
                entry["score"],
                entry["prev_hash"],
                entry["entry_hash"],
            ),
        )
        conn.commit()
        return entry
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def verify_chain(db_path: Optional[str] = None) -> list[dict]:
    """Walk the full chain from genesis forward; report every entry and any broken link.

    Returns a list of result dicts, one per entry, with keys:
        - ``id``: row id
        - ``entry_hash``: stored hash
        - ``computed_hash``: recomputed hash
        - ``prev_hash_ok``: whether prev_hash matches the previous entry's entry_hash
        - ``hash_ok``: whether the stored entry_hash matches the recomputed entry_hash
        - ``error``: description of any issue (None if the link is sound)

    If the chain is intact, every entry will have ``error=None`` and both
    ``prev_hash_ok`` and ``hash_ok`` = ``True``.
    """
    db_path = db_path or _default_db_path()
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, event_type, actor, wallet, score, prev_hash, entry_hash "
            "FROM audit_log ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    previous_entry_hash: Optional[str] = None

    for row in rows:
        (
            row_id,
            timestamp,
            event_type,
            actor,
            wallet,
            score,
            prev_hash,
            entry_hash,
        ) = row

        record = {
            "timestamp": timestamp,
            "event_type": event_type,
            "actor": actor,
            "wallet": wallet,
            "score": score,
            "prev_hash": prev_hash,
        }
        computed_hash = _compute_entry_hash(record)

        result: dict = {
            "id": row_id,
            "entry_hash": entry_hash,
            "computed_hash": computed_hash,
            "prev_hash_ok": True,
            "hash_ok": True,
            "error": None,
        }

        # Check stored entry_hash matches recomputed entry_hash
        if entry_hash != computed_hash:
            result["hash_ok"] = False
            result["error"] = (
                f"Entry {row_id}: stored entry_hash {entry_hash!r} "
                f"does not match recomputed hash {computed_hash!r}"
            )

        # Check prev_hash links to previous entry
        if previous_entry_hash is None:
            # First entry must have prev_hash == "genesis"
            if prev_hash != GENESIS_PREV_HASH:
                result["prev_hash_ok"] = False
                result["error"] = (
                    f"Entry {row_id} (genesis): expected prev_hash={GENESIS_PREV_HASH!r}, "
                    f"got {prev_hash!r}"
                )
        else:
            if prev_hash != previous_entry_hash:
                result["prev_hash_ok"] = False
                result["error"] = (
                    f"Entry {row_id}: prev_hash {prev_hash!r} does not match "
                    f"previous entry's entry_hash {previous_entry_hash!r}"
                )

        previous_entry_hash = entry_hash
        results.append(result)

    return results


def is_chain_intact(db_path: Optional[str] = None) -> bool:
    """Return ``True`` if the entire chain passes verification."""
    return all(r["error"] is None for r in verify_chain(db_path))


# ---------------------------------------------------------------------------
# Convenience event loggers
# ---------------------------------------------------------------------------


def log_score_computed(
    actor: str,
    wallet: str,
    score: int,
    db_path: Optional[str] = None,
) -> dict:
    """Log a score-computed event."""
    return append_entry("score_computed", actor, wallet=wallet, score=score, db_path=db_path)


def log_api_key_used(
    actor: str,
    db_path: Optional[str] = None,
) -> dict:
    """Log an API key usage event."""
    return append_entry("api_key_used", actor, db_path=db_path)


def log_admin_config_changed(
    actor: str,
    db_path: Optional[str] = None,
) -> dict:
    """Log an admin configuration change event."""
    return append_entry("admin_config_changed", actor, db_path=db_path)


def log_suppression_rule_added(
    actor: str,
    db_path: Optional[str] = None,
) -> dict:
    """Log a suppression rule addition."""
    return append_entry("suppression_rule_added", actor, db_path=db_path)


def log_suppression_rule_removed(
    actor: str,
    db_path: Optional[str] = None,
) -> dict:
    """Log a suppression rule removal."""
    return append_entry("suppression_rule_removed", actor, db_path=db_path)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_all_entries(db_path: Optional[str] = None) -> list[dict]:
    """Return all audit log entries ordered by id ascending."""
    db_path = db_path or _default_db_path()
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, event_type, actor, wallet, score, prev_hash, entry_hash "
            "FROM audit_log ORDER BY id ASC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "event_type": r[2],
                "actor": r[3],
                "wallet": r[4],
                "score": r[5],
                "prev_hash": r[6],
                "entry_hash": r[7],
            }
            for r in rows
        ]
    finally:
        conn.close()

        conn.close()
