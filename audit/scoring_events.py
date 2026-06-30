"""
Event Sourcing and Immutable Audit Log for Scoring Decisions  (Issue #297)
==========================================================================
Provides a cryptographically tamper-evident, append-only audit trail of every
scoring decision made by LedgerLens.

Design
------
* ``ScoringEvent`` — dataclass containing the full feature snapshot, model
  version, actor, and chain hash.
* ``ScoringEventStore`` — async SQLite-backed store; ``append`` computes the
  chain hash and inserts; ``replay`` returns events in chronological order.
* ``ChainHashVerifier`` — walks the event chain, recomputes each hash, and
  returns a ``ChainVerificationResult`` (VALID / TAMPERED / no_events).

Database
--------
The ``scoring_events`` table is append-only at the application layer, enforced
additionally by SQLite BEFORE UPDATE / BEFORE DELETE triggers.

Chain hash
----------
Each event's ``chain_hash`` is::

    SHA-256(canonical JSON({
        "prev": previous_chain_hash | "GENESIS",
        "event_id": ...,
        "wallet": ...,
        "score": ...,
        "features": {sorted feature_snapshot},
        "occurred_at": occurred_at.isoformat()
    }))

Security notes
--------------
* ``triggered_by`` is validated against an allowed set — it is NOT a free
  string; prevents sentinel injection.
* Admin override events *must* include a non-null ``actor_id``; the store
  rejects them at the application layer.
* The feature snapshot is included in the chain hash so retroactive feature
  modification invalidates the chain.
* Retention enforcement only runs when explicitly configured; default is 7
  years and never auto-deletes without operator opt-in.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger("ledgerlens.audit.scoring_events")

# ---------------------------------------------------------------------------
# Allowed triggered_by values (enum-like constant set)
# ---------------------------------------------------------------------------

VALID_TRIGGERED_BY: frozenset[str] = frozenset(
    {
        "ingestion",
        "manual_recompute",
        "feedback_boost",
        "admin_override",
    }
)

# ---------------------------------------------------------------------------
# ScoringEvent
# ---------------------------------------------------------------------------


@dataclass
class ScoringEvent:
    """A single immutable scoring decision record.

    Attributes
    ----------
    event_id:
        UUID v4 string, unique per event.
    wallet:
        Stellar wallet address that was scored.
    namespace_id:
        Namespace the wallet belongs to.
    score:
        Risk score 0–100 assigned by this decision.
    previous_score:
        Previous score (None for first-ever score of a wallet).
    feature_snapshot:
        Full ``FEATURE_NAMES → value`` mapping at the time of scoring.
        Included verbatim in the chain hash so retroactive modification
        is detectable.
    model_version:
        Content of ``models/model_version.txt`` at scoring time.
    triggered_by:
        What initiated the score: one of ``VALID_TRIGGERED_BY``.
    actor_id:
        API key ID that triggered the decision, or ``None`` for automated
        pipeline triggers.  Must be non-null when ``triggered_by ==
        "admin_override"``.
    chain_hash:
        SHA-256 hash linking this event to its predecessor for the wallet.
        Computed by ``ScoringEventStore.append``; do not set manually.
    occurred_at:
        UTC timestamp of the scoring decision.
    """

    event_id: str
    wallet: str
    namespace_id: str
    score: int
    previous_score: Optional[int]
    feature_snapshot: dict
    model_version: str
    triggered_by: str
    actor_id: Optional[str]
    chain_hash: str = field(default="")
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Chain hash computation
    # ------------------------------------------------------------------

    @staticmethod
    def compute_chain_hash(
        previous_chain_hash: Optional[str],
        event_id: str,
        wallet: str,
        score: int,
        feature_snapshot: dict,
        occurred_at: datetime,
    ) -> str:
        """Compute SHA-256 chain hash for a scoring event.

        The hash covers::

            {
                "prev": previous_chain_hash | "GENESIS",
                "event_id": event_id,
                "wallet": wallet,
                "score": score,
                "features": {keys sorted alphabetically},
                "occurred_at": occurred_at.isoformat()
            }

        Keys are sorted deterministically; the JSON is compact (no spaces).

        Returns
        -------
        str
            Lowercase hex digest.
        """
        payload = json.dumps(
            {
                "prev": previous_chain_hash if previous_chain_hash else "GENESIS",
                "event_id": event_id,
                "wallet": wallet,
                "score": score,
                "features": dict(sorted(feature_snapshot.items())),
                "occurred_at": occurred_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-safe)."""
        return {
            "event_id": self.event_id,
            "wallet": self.wallet,
            "namespace_id": self.namespace_id,
            "score": self.score,
            "previous_score": self.previous_score,
            "feature_snapshot": self.feature_snapshot,
            "model_version": self.model_version,
            "triggered_by": self.triggered_by,
            "actor_id": self.actor_id,
            "chain_hash": self.chain_hash,
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_row(cls, row: dict) -> "ScoringEvent":
        """Reconstruct from a SQLite row dict."""
        fs = row["feature_snapshot"]
        if isinstance(fs, str):
            try:
                fs = json.loads(fs)
            except Exception:
                fs = {}
        occurred = row["occurred_at"]
        if isinstance(occurred, str):
            try:
                occurred = datetime.fromisoformat(occurred)
            except Exception:
                occurred = datetime.now(timezone.utc)
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        return cls(
            event_id=row["event_id"],
            wallet=row["wallet"],
            namespace_id=row["namespace_id"],
            score=int(row["score"]),
            previous_score=row["previous_score"],
            feature_snapshot=fs,
            model_version=row["model_version"],
            triggered_by=row["triggered_by"],
            actor_id=row["actor_id"],
            chain_hash=row["chain_hash"],
            occurred_at=occurred,
        )


# ---------------------------------------------------------------------------
# DB schema & migration helper
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS scoring_events (
    event_id         TEXT PRIMARY KEY,
    wallet           TEXT NOT NULL,
    namespace_id     TEXT NOT NULL,
    score            INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    previous_score   INTEGER,
    feature_snapshot TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    triggered_by     TEXT NOT NULL,
    actor_id         TEXT,
    chain_hash       TEXT NOT NULL UNIQUE,
    occurred_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_se_wallet ON scoring_events (wallet, occurred_at);
CREATE INDEX IF NOT EXISTS idx_se_occurred_at ON scoring_events (occurred_at);

CREATE TRIGGER IF NOT EXISTS prevent_scoring_event_update
BEFORE UPDATE ON scoring_events
BEGIN
    SELECT RAISE(ABORT, 'scoring_events is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS prevent_scoring_event_delete
BEFORE DELETE ON scoring_events
BEGIN
    SELECT RAISE(ABORT, 'scoring_events is append-only: DELETE is not permitted');
END;
"""


async def init_scoring_events_db(db_path: str) -> None:
    """Create the ``scoring_events`` table and triggers if they don't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_DDL)
        await db.commit()


# ---------------------------------------------------------------------------
# ScoringEventStore
# ---------------------------------------------------------------------------


class ScoringEventStore:
    """Async SQLite-backed append-only store for ScoringEvents.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    max_feature_keys:
        Truncate feature_snapshot to this many keys if it exceeds the limit.
        Defaults to 50 (``AUDIT_FEATURE_SNAPSHOT_MAX_KEYS``).
    """

    def __init__(self, db_path: str, max_feature_keys: int = 50) -> None:
        self._db_path = db_path
        self._max_feature_keys = max_feature_keys

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        """Idempotent schema initialisation."""
        await init_scoring_events_db(self._db_path)

    async def _get_latest_event(self, wallet: str) -> Optional[ScoringEvent]:
        """Return the most recent event for ``wallet``, or None."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scoring_events WHERE wallet = ? ORDER BY occurred_at DESC LIMIT 1",
                (wallet,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return ScoringEvent.from_row(dict(row))

    async def _insert(self, event: ScoringEvent) -> None:
        """Insert a ScoringEvent row (no UPDATE or DELETE ever called)."""
        fs_json = json.dumps(
            dict(sorted(event.feature_snapshot.items())), separators=(",", ":")
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO scoring_events
                    (event_id, wallet, namespace_id, score, previous_score,
                     feature_snapshot, model_version, triggered_by, actor_id,
                     chain_hash, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.wallet,
                    event.namespace_id,
                    event.score,
                    event.previous_score,
                    fs_json,
                    event.model_version,
                    event.triggered_by,
                    event.actor_id,
                    event.chain_hash,
                    event.occurred_at.isoformat(),
                ),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, event: ScoringEvent) -> None:
        """Append a ScoringEvent to the store.

        Validates:
        * ``triggered_by`` is one of ``VALID_TRIGGERED_BY``.
        * ``actor_id`` is non-null for ``admin_override`` events.
        * Feature snapshot is truncated to ``max_feature_keys`` if oversized.

        Computes ``chain_hash`` by chaining to the previous event's hash for
        this wallet (or uses GENESIS sentinel for the first event).

        Raises
        ------
        ValueError
            If ``triggered_by`` is invalid or ``actor_id`` is missing for an
            admin override.
        """
        await self._ensure_schema()

        if event.triggered_by not in VALID_TRIGGERED_BY:
            raise ValueError(
                f"Invalid triggered_by value {event.triggered_by!r}. "
                f"Must be one of {sorted(VALID_TRIGGERED_BY)}."
            )
        if event.triggered_by == "admin_override" and not event.actor_id:
            raise ValueError(
                "admin_override events must include a non-null actor_id."
            )

        # Truncate feature snapshot if oversized
        if len(event.feature_snapshot) > self._max_feature_keys:
            logger.warning(
                "Feature snapshot for event %s has %d keys; truncating to %d.",
                event.event_id,
                len(event.feature_snapshot),
                self._max_feature_keys,
            )
            event.feature_snapshot = dict(
                list(event.feature_snapshot.items())[: self._max_feature_keys]
            )

        prev = await self._get_latest_event(event.wallet)
        event.chain_hash = ScoringEvent.compute_chain_hash(
            previous_chain_hash=prev.chain_hash if prev else None,
            event_id=event.event_id,
            wallet=event.wallet,
            score=event.score,
            feature_snapshot=event.feature_snapshot,
            occurred_at=event.occurred_at,
        )
        await self._insert(event)
        logger.debug(
            "Appended scoring event %s for wallet %s (score=%d).",
            event.event_id[:8],
            event.wallet[:8],
            event.score,
        )

    async def replay(
        self,
        wallet: str,
        since: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[ScoringEvent]:
        """Return all events for ``wallet`` in chronological order (oldest first).

        Parameters
        ----------
        wallet:
            Wallet address to query.
        since:
            If provided, only return events on or after this UTC timestamp.
        limit:
            Maximum number of events to return (default 1000).
        """
        await self._ensure_schema()
        params: list = [wallet]
        query = "SELECT * FROM scoring_events WHERE wallet = ?"
        if since is not None:
            query += " AND occurred_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY occurred_at ASC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [ScoringEvent.from_row(dict(row)) for row in rows]

    async def current_score(self, wallet: str) -> Optional[int]:
        """Return the score from the most recent event, or None."""
        latest = await self._get_latest_event(wallet)
        return latest.score if latest else None

    async def summary(self, hours: int = 24) -> dict:
        """Return aggregate statistics for the audit summary endpoint.

        Returns a dict with:
        * ``events_last_24h`` — count of events in the last ``hours`` hours.
        * ``unique_wallets_scored`` — distinct wallets scored in that window.
        * ``integrity_violations`` — always 0 (full verification is separate).
        """
        await self._ensure_schema()
        cutoff = datetime.now(timezone.utc)
        from datetime import timedelta
        since = (cutoff - timedelta(hours=hours)).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM scoring_events WHERE occurred_at >= ?",
                (since,),
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            async with db.execute(
                "SELECT COUNT(DISTINCT wallet) FROM scoring_events WHERE occurred_at >= ?",
                (since,),
            ) as cur:
                row = await cur.fetchone()
                unique = row[0] if row else 0

        return {
            "events_last_24h": count,
            "unique_wallets_scored": unique,
            "integrity_violations": 0,
        }


# ---------------------------------------------------------------------------
# Chain hash verifier
# ---------------------------------------------------------------------------


@dataclass
class ChainVerificationResult:
    """Result of verifying the event chain for a single wallet.

    Attributes
    ----------
    wallet:
        The wallet that was verified.
    status:
        ``"valid"`` | ``"tampered"`` | ``"no_events"``.
    total_events:
        Number of events inspected.
    first_tampered_event_id:
        Event ID of the first hash mismatch, or None.
    verified_at:
        UTC timestamp of the verification run.
    """

    wallet: str
    status: str
    total_events: int
    first_tampered_event_id: Optional[str]
    verified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "status": self.status,
            "total_events": self.total_events,
            "first_tampered_event_id": self.first_tampered_event_id,
            "verified_at": self.verified_at.isoformat(),
        }


class ChainHashVerifier:
    """Verifies the chain-hash integrity of a wallet's scoring event history.

    Parameters
    ----------
    store:
        A ``ScoringEventStore`` instance to replay events from.
    """

    def __init__(self, store: ScoringEventStore) -> None:
        self._store = store

    async def verify(self, wallet: str) -> ChainVerificationResult:
        """Walk events for ``wallet`` in chronological order.

        Recomputes each event's chain hash from its fields and the previous
        event's hash.  Returns VALID if all match, TAMPERED (with the first
        failing event_id) if any mismatch is found.

        Returns
        -------
        ChainVerificationResult
            ``status="no_events"`` when no events exist for the wallet.
        """
        events = await self._store.replay(wallet)
        if not events:
            return ChainVerificationResult(
                wallet=wallet,
                status="no_events",
                total_events=0,
                first_tampered_event_id=None,
            )

        prev_hash: Optional[str] = None
        for event in events:
            expected = ScoringEvent.compute_chain_hash(
                previous_chain_hash=prev_hash,
                event_id=event.event_id,
                wallet=event.wallet,
                score=event.score,
                feature_snapshot=event.feature_snapshot,
                occurred_at=event.occurred_at,
            )
            if expected != event.chain_hash:
                logger.warning(
                    "Chain hash MISMATCH for wallet %s at event %s",
                    wallet[:8],
                    event.event_id,
                )
                return ChainVerificationResult(
                    wallet=wallet,
                    status="tampered",
                    total_events=len(events),
                    first_tampered_event_id=event.event_id,
                )
            prev_hash = event.chain_hash

        return ChainVerificationResult(
            wallet=wallet,
            status="valid",
            total_events=len(events),
            first_tampered_event_id=None,
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def make_scoring_event(
    wallet: str,
    namespace_id: str,
    score: int,
    previous_score: Optional[int],
    feature_snapshot: dict,
    model_version: str,
    triggered_by: str,
    actor_id: Optional[str] = None,
) -> ScoringEvent:
    """Create a ScoringEvent with a freshly generated UUID v4 event_id."""
    return ScoringEvent(
        event_id=str(uuid.uuid4()),
        wallet=wallet,
        namespace_id=namespace_id,
        score=score,
        previous_score=previous_score,
        feature_snapshot=feature_snapshot,
        model_version=model_version,
        triggered_by=triggered_by,
        actor_id=actor_id,
    )
