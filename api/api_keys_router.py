"""API Key Management with scoped permissions and per-key rate limits (Issue #195)."""

import hashlib
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from api.auth import require_admin_key
from config.settings import settings

router = APIRouter(prefix="/admin/api-keys", tags=["API Keys"])

VALID_SCOPES = {"read:scores", "write:suppressions", "admin"}

# In-memory sliding window counters: {key_hash: [timestamps]}
_rate_windows: dict[str, list[float]] = {}


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL UNIQUE,
            namespace_id TEXT NOT NULL DEFAULT '',
            scopes TEXT NOT NULL DEFAULT 'read:scores',
            rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def get_api_key_record(key_hash: str) -> Optional[dict]:
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0",
            (key_hash,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def check_rate_limit(key_hash: str, limit: int) -> None:
    now = time.monotonic()
    window = _rate_windows.setdefault(key_hash, [])
    _rate_windows[key_hash] = [t for t in window if now - t < 60.0]
    if len(_rate_windows[key_hash]) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Per-key rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    _rate_windows[key_hash].append(now)


def require_scope(required_scope: str):
    def dependency(x_api_key: str = Header(default="")) -> dict:
        if not x_api_key:
            raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

        key_hash = _hash_key(x_api_key)
        record = get_api_key_record(key_hash)

        if record is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        if record.get("expires_at"):
            now_iso = datetime.now(timezone.utc).isoformat()
            if record["expires_at"] < now_iso:
                raise HTTPException(status_code=401, detail="API key has expired")

        check_rate_limit(key_hash, record["rate_limit_per_minute"])

        scopes = set(s.strip() for s in record["scopes"].split(",") if s.strip())
        if required_scope not in scopes and "admin" not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Scope '{required_scope}' is required for this endpoint",
            )

        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                (datetime.now(timezone.utc).isoformat(), key_hash),
            )

        return record

    return dependency


class CreateKeyRequest(BaseModel):
    namespace_id: str = ""
    scopes: list[str] = ["read:scores"]
    rate_limit_per_minute: int = 60
    expires_at: Optional[str] = None


class CreateKeyResponse(BaseModel):
    id: int
    plaintext_key: str
    namespace_id: str
    scopes: list[str]
    rate_limit_per_minute: int
    created_at: str
    expires_at: Optional[str]


@router.post(
    "",
    response_model=CreateKeyResponse,
    status_code=201,
    summary="Create a new API key",
    description=(
        "Creates a scoped API key. The plaintext key is returned once and never stored. "
        "Valid scopes: read:scores, write:suppressions, admin."
    ),
    dependencies=[Depends(require_admin_key)],
)
def create_api_key(body: CreateKeyRequest) -> CreateKeyResponse:
    for scope in body.scopes:
        if scope not in VALID_SCOPES:
            raise HTTPException(status_code=422, detail=f"Unknown scope: {scope!r}")

    plaintext = secrets.token_urlsafe(32)
    key_hash = _hash_key(plaintext)
    now = datetime.now(timezone.utc).isoformat()
    scopes_str = ",".join(body.scopes)

    with sqlite3.connect(settings.db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            """
            INSERT INTO api_keys (key_hash, namespace_id, scopes, rate_limit_per_minute, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key_hash, body.namespace_id, scopes_str, body.rate_limit_per_minute, now, body.expires_at),
        )
        row_id = cur.lastrowid

    return CreateKeyResponse(
        id=row_id,
        plaintext_key=plaintext,
        namespace_id=body.namespace_id,
        scopes=body.scopes,
        rate_limit_per_minute=body.rate_limit_per_minute,
        created_at=now,
        expires_at=body.expires_at,
    )


@router.delete(
    "/{key_id}",
    summary="Revoke an API key",
    description="Immediately revokes a key. Subsequent requests with this key return 401.",
    dependencies=[Depends(require_admin_key)],
)
def revoke_api_key(key_id: int) -> dict:
    with sqlite3.connect(settings.db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0",
            (key_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"API key {key_id} not found or already revoked")

    _rate_windows.pop(key_id, None)
    return {"revoked": True, "id": key_id}
