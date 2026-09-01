"""Immutable, append-only audit log with HMAC-SHA256 chain validation.

Each entry is linked to the previous via ``prev_hash``, forming a tamper-evident
chain. The ``entry_hash`` of every row is computed as::

    entry_hash = HMAC-SHA256(key=AUDIT_SECRET, msg=canonical_json(entry_without_hash))

where ``canonical_json`` produces a deterministic, sorted-key JSON encoding of
all columns *except* ``entry_hash``.

A ``genesis`` entry (prev_hash="genesis") is automatically inserted when the
table is first created.

Signing secret
--------------
The HMAC key comes from ``LEDGERLENS_AUDIT_SECRET`` and has no hardcoded
fallback. It previously defaulted to a constant committed to this repository,
which meant any deployment that omitted the variable produced a chain whose
HMACs any reader of the public source could recompute -- tamper-evident in
form, but providing no actual tamper evidence.

Resolution order:

* ``LEDGERLENS_AUDIT_SECRET`` set (>= 32 chars) -- used as-is.
* Otherwise, in a production-flagged environment (``LEDGERLENS_ENV`` in
  {production, prod, staging}, or ``NETWORK=mainnet``) -- raise
  :class:`AuditSecretError` and refuse to run.
* Otherwise (local development) -- generate a random secret once and persist it
  to a machine-local, gitignored file beside the database. Random rather than
  constant, so it is not derivable from the source tree; persisted rather than
  per-process, so a chain written before a restart still verifies after it.

Key rotation
------------
The chain is signed, not merely hashed, so **rotating the secret invalidates
verification of every entry written under the old key**. There is no way around
this without re-signing history, which would defeat the point of the chain. The
supported procedure is therefore to seal rather than re-sign:

1. Stop writers and run ``verify_chain`` under the *old* secret; keep the result
   as the integrity attestation for that segment.
2. Archive the existing ``audit_log.db`` read-only alongside that attestation
   and a note of the rotation time.
3. Start a fresh chain (new genesis entry) under the new secret.

Verifying pre-rotation history then means verifying the archived segment with
the archived key. Treat a rotation as a compliance event and record it.

Event types logged:
    - ``score_computed``       — a risk score was computed for a wallet
    - ``api_key_used``         — an API key was used to access the system
    - ``admin_config_changed`` — an admin changed a configuration value
    - ``suppression_rule_added``   — a suppression rule was added
    - ``suppression_rule_removed`` — a suppression rule was removed
    - ``audit_chain_verified`` — the full chain was verified (integrity self-check)

Relationship to other audit modules: this module is the general-purpose,
system-wide audit trail (API key usage, admin config changes, suppression
rule changes, and a coarse ``score_computed`` marker) chained with
HMAC-SHA256. It is distinct from ``audit.scoring_events``, which is a
dedicated, event-sourced audit log specifically for scoring *decisions* —
it stores the full feature snapshot and model version behind each score
and supports replaying the score from its events; see ``docs/audit_log.md``
for its design rationale and chain-hash specification.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import stat
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

# Which deployments must refuse to start without a real signing secret.
# NETWORK=mainnet is honoured too, so an existing mainnet deployment is
# covered without having to also set LEDGERLENS_ENV.
ENVIRONMENT_ENV_KEY = "LEDGERLENS_ENV"
NETWORK_ENV_KEY = "NETWORK"
_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod", "staging"})

# Filename of the machine-local development secret. Generated on first use and
# never committed — see the module docstring.
DEV_SECRET_FILENAME = ".ledgerlens_audit_secret"

# Short keys weaken the HMAC and are almost always a placeholder that escaped
# review; 32 characters is the smallest value worth trusting here.
MIN_AUDIT_SECRET_LENGTH = 32

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class AuditSecretError(RuntimeError):
    """Raised when no trustworthy audit-chain signing secret is available.

    Deliberately fatal. A tamper-evident chain signed with a key an attacker
    can obtain is not tamper-evident, so continuing without a real secret would
    produce audit history that merely looks verified.
    """


def is_production_environment() -> bool:
    """True when this deployment must not fall back to a generated secret."""
    env = os.getenv(ENVIRONMENT_ENV_KEY, "").strip().lower()
    if env in _PRODUCTION_ENVIRONMENTS:
        return True
    return os.getenv(NETWORK_ENV_KEY, "").strip().lower() == "mainnet"


def _dev_secret_path() -> Path:
    """Machine-local path for the generated development secret."""
    return Path(_default_db_path()).parent / DEV_SECRET_FILENAME


def _load_or_create_dev_secret() -> str:
    """Return a persistent, machine-local, randomly generated dev secret.

    Persisted rather than per-process so that a chain written before a restart
    still verifies afterwards, and generated rather than hardcoded so that it
    is not derivable from the public source tree.
    """
    path = _dev_secret_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:  # unreadable path — fall through to a fresh secret
        logger.warning("Could not read %s (%s); generating a fresh one", path, exc)

    secret = secrets.token_urlsafe(48)
    try:
        path.write_text(secret, encoding="utf-8")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        # An ephemeral secret still beats a public constant; it only means the
        # chain will not verify across restarts on this machine.
        logger.warning("Could not persist dev audit secret to %s (%s)", path, exc)
    logger.warning(
        "%s is not set. Generated a machine-local development audit secret at %s. "
        "This is for local use only; set %s explicitly in any shared or "
        "production deployment.",
        AUDIT_SECRET_ENV_KEY,
        path,
        AUDIT_SECRET_ENV_KEY,
    )
    return secret


def _get_audit_secret() -> bytes:
    """Return the HMAC key, failing fast rather than using a guessable default.

    The previous implementation fell back to a constant committed to this
    repository, so any deployment that omitted the environment variable
    produced a chain whose HMACs any reader of the public source could
    recompute — tamper-evident in form only.
    """
    secret = os.getenv(AUDIT_SECRET_ENV_KEY, "").strip()

    if secret:
        if len(secret) < MIN_AUDIT_SECRET_LENGTH:
            raise AuditSecretError(
                f"{AUDIT_SECRET_ENV_KEY} must be at least "
                f"{MIN_AUDIT_SECRET_LENGTH} characters; got {len(secret)}."
            )
        return secret.encode("utf-8")

    if is_production_environment():
        raise AuditSecretError(
            f"{AUDIT_SECRET_ENV_KEY} is required when {ENVIRONMENT_ENV_KEY} is a "
            f"production environment or {NETWORK_ENV_KEY}=mainnet. Refusing to "
            f"sign the audit chain with a generated key: audit history signed "
            f"with a key that is not under operator control cannot be trusted. "
            f"Generate one with: python -c \"import secrets; "
            f"print(secrets.token_urlsafe(48))\""
        )

    return _load_or_create_dev_secret().encode("utf-8")


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
