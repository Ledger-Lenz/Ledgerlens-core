"""Wallet allowlist and denylist management with audit trail (Issue #181)."""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings

router = APIRouter(prefix="/admin", tags=["Allowlist / Denylist"])

LIST_TYPES = {"allowlist", "denylist"}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wallet_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            list_type TEXT NOT NULL CHECK(list_type IN ('allowlist','denylist')),
            reason TEXT NOT NULL DEFAULT '',
            added_by TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            removed_at TEXT,
            removed_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wallet_overrides_wallet
            ON wallet_overrides (wallet);
        CREATE INDEX IF NOT EXISTS idx_wallet_overrides_list_type
            ON wallet_overrides (list_type);
        """
    )


def get_active_override(wallet: str) -> Optional[dict]:
    """Return the active override row for wallet, or None."""
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        row = conn.execute(
            """
            SELECT * FROM wallet_overrides
            WHERE wallet = ? AND removed_at IS NULL
            ORDER BY added_at DESC LIMIT 1
            """,
            (wallet,),
        ).fetchone()
        return dict(row) if row else None


class OverrideRequest(BaseModel):
    wallet: str
    reason: str = ""
    added_by: str = ""


def _add_override(list_type: str, body: OverrideRequest) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        # Soft-remove any existing active entry first
        conn.execute(
            "UPDATE wallet_overrides SET removed_at = ?, removed_by = 'system:replaced' "
            "WHERE wallet = ? AND list_type = ? AND removed_at IS NULL",
            (now, body.wallet, list_type),
        )
        cur = conn.execute(
            """
            INSERT INTO wallet_overrides (wallet, list_type, reason, added_by, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (body.wallet, list_type, body.reason, body.added_by, now),
        )
        row = conn.execute(
            "SELECT * FROM wallet_overrides WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def _list_overrides(list_type: str, page: int, page_size: int) -> list[dict]:
    offset = (page - 1) * page_size
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT * FROM wallet_overrides
            WHERE list_type = ?
            ORDER BY added_at DESC
            LIMIT ? OFFSET ?
            """,
            (list_type, page_size, offset),
        ).fetchall()
        return [dict(r) for r in rows]


@router.post(
    "/allowlist",
    status_code=201,
    summary="Add wallet to allowlist",
    description="Allowlisted wallets return score=0 with override='allowlisted' immediately.",
    dependencies=[Depends(require_admin_key)],
)
def add_to_allowlist(body: OverrideRequest) -> dict:
    return _add_override("allowlist", body)


@router.post(
    "/denylist",
    status_code=201,
    summary="Add wallet to denylist",
    description="Denylisted wallets return score=100 with override='denylisted' immediately.",
    dependencies=[Depends(require_admin_key)],
)
def add_to_denylist(body: OverrideRequest) -> dict:
    return _add_override("denylist", body)


@router.get(
    "/allowlist",
    summary="List allowlist entries",
    description="Returns all allowlist entries (including soft-deleted) with pagination.",
    dependencies=[Depends(require_admin_key)],
)
def list_allowlist(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return _list_overrides("allowlist", page, page_size)


@router.get(
    "/denylist",
    summary="List denylist entries",
    description="Returns all denylist entries (including soft-deleted) with pagination.",
    dependencies=[Depends(require_admin_key)],
)
def list_denylist(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return _list_overrides("denylist", page, page_size)


@router.delete(
    "/allowlist/{wallet}",
    summary="Remove wallet from allowlist",
    description="Soft-deletes the allowlist entry; history is preserved with removed_at timestamp.",
    dependencies=[Depends(require_admin_key)],
)
def remove_from_allowlist(wallet: str, removed_by: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE wallet_overrides SET removed_at = ?, removed_by = ? "
            "WHERE wallet = ? AND list_type = 'allowlist' AND removed_at IS NULL",
            (now, removed_by or "unknown", wallet),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Wallet {wallet!r} not in allowlist")
    return {"removed": True, "wallet": wallet, "removed_at": now}


@router.delete(
    "/denylist/{wallet}",
    summary="Remove wallet from denylist",
    description="Soft-deletes the denylist entry; history is preserved with removed_at timestamp.",
    dependencies=[Depends(require_admin_key)],
)
def remove_from_denylist(wallet: str, removed_by: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(settings.db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE wallet_overrides SET removed_at = ?, removed_by = ? "
            "WHERE wallet = ? AND list_type = 'denylist' AND removed_at IS NULL",
            (now, removed_by or "unknown", wallet),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Wallet {wallet!r} not in denylist")
    return {"removed": True, "wallet": wallet, "removed_at": now}
