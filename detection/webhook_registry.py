"""Subscriber registry for webhook alerts — SQLite-backed with AES-256-GCM at-rest encryption.

Secrets are never hashed (HMAC signing requires the plaintext at delivery time).
Encryption key loaded from ``LEDGERLENS_WEBHOOK_ENCRYPTION_KEY`` (32-byte base64).
SSRF protection: only ``https://`` URLs, no private IPs, no localhost.
"""

import base64
import logging
import os
import re
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import settings
from detection.risk_score import RiskScore

logger = logging.getLogger("ledgerlens.webhook.registry")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    secret_encrypted TEXT NOT NULL,
    min_score INTEGER NOT NULL DEFAULT 70,
    wallet_filter TEXT,
    asset_pair_filter TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_subscribers_active ON webhook_subscribers (active);
"""


@dataclass
class Subscriber:
    subscriber_id: str
    url: str
    secret: str
    min_score: int
    wallet_filter: list[str] | None = None
    asset_pair_filter: list[str] | None = None
    active: bool = True
    created_at: str = ""

    def masked_secret(self) -> str:
        s = self.secret
        if len(s) <= 8:
            return s[:2] + "****"
        return s[:4] + "****"


# ---------------------------------------------------------------------------
# Encryption helpers  (AES-256-GCM)
# ---------------------------------------------------------------------------

def _get_encryption_key() -> bytes:
    key_b64 = os.environ.get("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY")
    if not key_b64:
        raise RuntimeError(
            "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY environment variable not set. "
            "Generate one with: python -c \"import base64, os; "
            "print(base64.b64encode(os.urandom(32)).decode())\""
        )
    return base64.b64decode(key_b64)


def _encrypt_secret(plaintext: str) -> str:
    key = _get_encryption_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_secret(encrypted: str) -> str:
    key = _get_encryption_key()
    data = base64.b64decode(encrypted)
    nonce, ct = data[:12], data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_IPV4_PRIVATE = re.compile(r"^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)")
_IPV6_PRIVATE = re.compile(r"^f[cd]", re.IGNORECASE)
_LOOPBACK = re.compile(r"^127\.\d+\.\d+\.\d+$")


def _resolve_hostname(hostname: str) -> str:
    try:
        return socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0]
    except socket.gaierror:
        raise ValueError(
            f"Hostname '{hostname}' could not be resolved — URL is not reachable"
        )


def validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"URL scheme must be https, got '{parsed.scheme}'")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a hostname")
    if hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Localhost URLs are not allowed (SSRF protection)")

    addr = _resolve_hostname(hostname)
    if addr == "::1":
        raise ValueError("Localhost URLs are not allowed (SSRF protection)")
    if "." in addr:
        if _LOOPBACK.match(addr):
            raise ValueError("Localhost URLs are not allowed (SSRF protection)")
        if _IPV4_PRIVATE.match(addr):
            raise ValueError("Private IP URLs are not allowed (SSRF protection)")
    elif _IPV6_PRIVATE.match(addr):
        raise ValueError("Private IP URLs are not allowed (SSRF protection)")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None):
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _row_to_subscriber(row) -> Subscriber:
    return Subscriber(
        subscriber_id=row[0],
        url=row[1],
        secret=_decrypt_secret(row[2]),
        min_score=row[3],
        wallet_filter=row[4].split(",") if row[4] else None,
        asset_pair_filter=row[5].split(",") if row[5] else None,
        active=bool(row[6]),
        created_at=row[7],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_subscriber(
    url: str,
    secret: str,
    min_score: int = 70,
    wallet_filter: str | None = None,
    asset_pair_filter: str | None = None,
    db_path: str | None = None,
) -> str:
    init_db(db_path)
    validate_webhook_url(url)
    subscriber_id = str(uuid.uuid4())
    secret_encrypted = _encrypt_secret(secret)
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO webhook_subscribers (subscriber_id, url, secret_encrypted, min_score, wallet_filter, asset_pair_filter, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (subscriber_id, url, secret_encrypted, min_score, wallet_filter, asset_pair_filter, created_at),
        )
        conn.commit()
    return subscriber_id


def get_subscriber(subscriber_id: str, db_path: str | None = None) -> Subscriber | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT subscriber_id, url, secret_encrypted, min_score, wallet_filter, asset_pair_filter, active, created_at FROM webhook_subscribers WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
        return _row_to_subscriber(row) if row else None


def list_subscribers(active_only: bool = True, db_path: str | None = None) -> list[Subscriber]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if active_only:
            rows = conn.execute(
                "SELECT subscriber_id, url, secret_encrypted, min_score, wallet_filter, asset_pair_filter, active, created_at FROM webhook_subscribers WHERE active = 1 ORDER BY created_at"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT subscriber_id, url, secret_encrypted, min_score, wallet_filter, asset_pair_filter, active, created_at FROM webhook_subscribers ORDER BY created_at"
            ).fetchall()
        return [_row_to_subscriber(r) for r in rows]


def deactivate_subscriber(subscriber_id: str, db_path: str | None = None) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE webhook_subscribers SET active = 0 WHERE subscriber_id = ? AND active = 1",
            (subscriber_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_matching_subscribers(score: RiskScore, db_path: str | None = None) -> list[Subscriber]:
    subscribers = list_subscribers(active_only=True, db_path=db_path)
    return [
        sub
        for sub in subscribers
        if score.score >= sub.min_score
        and (not sub.wallet_filter or score.wallet in sub.wallet_filter)
        and (not sub.asset_pair_filter or score.asset_pair in sub.asset_pair_filter)
    ]
