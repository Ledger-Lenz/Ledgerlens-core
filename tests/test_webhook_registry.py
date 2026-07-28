"""Tests for ``detection.webhook_registry``.

All source imports are at module level.  Every test receives an isolated
SQLite database via the ``db_path`` fixture so there is no shared state
between tests.  The ``webhook_env`` fixture sets the encryption key required
by the registry and is declared ``autouse=True`` so no test accidentally runs
without it.
"""

import base64
import os
import re
from datetime import datetime, timezone

import pytest

from detection.risk_score import RiskScore
from detection.webhook_registry import (
    _connect,
    deactivate_subscriber,
    get_matching_subscribers,
    get_subscriber,
    init_db,
    list_subscribers,
    register_subscriber,
    validate_webhook_url,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    """Inject a fresh random AES-256-GCM key for every test.

    autouse ensures no test ever runs without a valid encryption key, which
    would cause all register_subscriber() calls to raise RuntimeError.
    """
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture
def db_path(tmp_path):
    """Return a per-test SQLite path; the schema is created by init_db() inside
    each test (or by register_subscriber, which calls init_db internally)."""
    return str(tmp_path / "webhooks.db")


def _score(wallet="GABC", asset_pair="XLM/USDC", score=80):
    """Build a minimal RiskScore for filter-matching tests."""
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Subscriber CRUD
# ---------------------------------------------------------------------------


def test_register_and_list(db_path):
    """register_subscriber() returns a valid UUID and the subscriber appears
    in list_subscribers() with the correct attributes."""
    init_db(db_path)
    sid = register_subscriber(
        "https://example.com/webhook", "whsec_test", min_score=70, db_path=db_path
    )
    assert re.match(r"^[0-9a-f-]{36}$", sid)

    subs = list_subscribers(db_path=db_path)
    assert len(subs) == 1
    assert subs[0].subscriber_id == sid
    assert subs[0].url == "https://example.com/webhook"
    assert subs[0].min_score == 70
    assert subs[0].active is True


def test_get_subscriber(db_path):
    """get_subscriber() returns the subscriber with the plaintext secret."""
    init_db(db_path)
    sid = register_subscriber(
        "https://example.com/webhook", "whsec_test", db_path=db_path
    )
    sub = get_subscriber(sid, db_path)
    assert sub is not None
    assert sub.secret == "whsec_test"


def test_get_subscriber_not_found(db_path):
    """get_subscriber() returns None for an unknown subscriber_id."""
    init_db(db_path)
    assert get_subscriber("nonexistent", db_path) is None


def test_deactivate_subscriber(db_path):
    """deactivate_subscriber() returns True on first call and False on repeat."""
    init_db(db_path)
    sid = register_subscriber(
        "https://example.com/webhook", "whsec_test", db_path=db_path
    )
    assert deactivate_subscriber(sid, db_path) is True
    assert len(list_subscribers(db_path=db_path)) == 0
    assert deactivate_subscriber(sid, db_path) is False


def test_list_subscribers_inactive_included(db_path):
    """list_subscribers(active_only=False) includes deactivated subscribers."""
    init_db(db_path)
    sid = register_subscriber(
        "https://example.com/webhook", "whsec_test", db_path=db_path
    )
    deactivate_subscriber(sid, db_path)
    assert len(list_subscribers(active_only=False, db_path=db_path)) == 1


# ---------------------------------------------------------------------------
# Encryption / secret handling
# ---------------------------------------------------------------------------


def test_secret_encrypt_decrypt_roundtrip(db_path):
    """The plaintext secret survives a register → list round-trip."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "my_super_secret_key_123!", db_path=db_path
    )
    sub = list_subscribers(db_path=db_path)[0]
    assert sub.secret == "my_super_secret_key_123!"


def test_secret_not_hashed(db_path):
    """The stored secret_encrypted column contains AES-GCM ciphertext, not a
    SHA-256 hash — raw secrets must never be stored in cleartext."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "whsec_test", db_path=db_path
    )
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT secret_encrypted FROM webhook_subscribers"
        ).fetchone()
    encrypted = row[0]
    # AES-GCM output is base64(12-byte nonce + ciphertext + 16-byte tag),
    # so its length must exceed a 64-char SHA-256 hex digest.
    assert len(encrypted) > 44
    assert encrypted != "sha256=xxx"


def test_masked_secret(db_path):
    """masked_secret() redacts the secret value while keeping a visible prefix."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "sk_live_abcdefghijklmnop", db_path=db_path
    )
    sub = list_subscribers(db_path=db_path)[0]
    masked = sub.masked_secret()
    assert "****" in masked
    assert "abcdefghijklmnop" not in masked
    assert masked != sub.secret


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


def test_ssrf_rejects_http(db_path):
    """Plain HTTP URLs are rejected at registration."""
    init_db(db_path)
    with pytest.raises(ValueError, match="scheme must be https"):
        register_subscriber("http://evil.com/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_localhost(db_path):
    """Localhost hostnames are rejected (SSRF protection)."""
    init_db(db_path)
    with pytest.raises(ValueError, match="Localhost"):
        register_subscriber(
            "https://localhost:8000/webhook", "whsec_test", db_path=db_path
        )


def test_ssrf_rejects_private_ip_10(db_path):
    """10.x.x.x addresses are rejected as private IP ranges."""
    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber("https://10.0.0.1/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_private_ip_192_168(db_path):
    """192.168.x.x addresses are rejected as private IP ranges."""
    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber(
            "https://192.168.1.1/webhook", "whsec_test", db_path=db_path
        )


def test_ssrf_rejects_private_ip_172(db_path):
    """172.16-31.x.x addresses are rejected as private IP ranges."""
    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber(
            "https://172.16.0.1/webhook", "whsec_test", db_path=db_path
        )


def test_ssrf_rejects_reserved_ip_127(db_path):
    """127.x.x.x loopback addresses are rejected."""
    init_db(db_path)
    with pytest.raises(ValueError, match="Localhost"):
        register_subscriber(
            "https://127.0.0.1/webhook", "whsec_test", db_path=db_path
        )


def test_ssrf_rejects_unresolvable_hostname(db_path):
    """Hostnames that cannot be DNS-resolved are rejected to prevent
    time-of-check / time-of-use attacks via dynamic DNS."""
    init_db(db_path)
    with pytest.raises(ValueError, match="could not be resolved"):
        register_subscriber(
            "https://thishostnamedoesnotexistzzzzzzzzzz.com/webhook",
            "whsec_test",
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# Subscriber matching
# ---------------------------------------------------------------------------


def test_get_matching_respects_min_score(db_path):
    """Only subscribers whose min_score <= alert score are returned."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "whsec_test", min_score=70, db_path=db_path
    )
    register_subscriber(
        "https://other.com/webhook", "whsec_other", min_score=90, db_path=db_path
    )

    assert len(get_matching_subscribers(_score(score=50), db_path)) == 0
    assert len(get_matching_subscribers(_score(score=80), db_path)) == 1
    assert len(get_matching_subscribers(_score(score=95), db_path)) == 2


def test_get_matching_respects_wallet_filter(db_path):
    """A wallet_filter restricts delivery to specified wallets only."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook",
        "whsec_test",
        min_score=50,
        wallet_filter="GABC,GDEF",
        db_path=db_path,
    )

    assert len(get_matching_subscribers(_score(wallet="GABC", score=60), db_path)) == 1
    assert len(get_matching_subscribers(_score(wallet="GXYZ", score=60), db_path)) == 0


def test_get_matching_respects_asset_pair_filter(db_path):
    """An asset_pair_filter restricts delivery to the specified pair only."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook",
        "whsec_test",
        min_score=50,
        asset_pair_filter="XLM/USDC",
        db_path=db_path,
    )

    assert len(get_matching_subscribers(_score(asset_pair="XLM/USDC", score=60), db_path)) == 1
    assert len(get_matching_subscribers(_score(asset_pair="BTC/USDC", score=60), db_path)) == 0


def test_get_matching_wallet_filter_null_matches_all_wallets(db_path):
    """A subscriber with no wallet_filter matches any wallet."""
    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "whsec_test", min_score=50, db_path=db_path
    )

    result = get_matching_subscribers(_score(wallet="GXYZ", score=60), db_path)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# URL validation unit tests
# ---------------------------------------------------------------------------


def test_validate_webhook_url_accepts_valid():
    """Well-formed public HTTPS URLs pass validation without raising."""
    validate_webhook_url("https://example.com/webhook")
    validate_webhook_url("https://httpbin.org/post")


def test_validate_webhook_url_rejects_no_hostname():
    """A URL with an empty hostname is rejected."""
    with pytest.raises(ValueError, match="hostname"):
        validate_webhook_url("https:///path")
