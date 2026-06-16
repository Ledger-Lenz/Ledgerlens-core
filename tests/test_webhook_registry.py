"""Tests for ``detection.webhook_registry``."""

import base64
import os
import re

import pytest

from detection.risk_score import RiskScore


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "webhooks.db")


def _score(wallet="GABC", asset_pair="XLM/USDC", score=80):
    from datetime import datetime, timezone

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
    from detection.webhook_registry import init_db, list_subscribers, register_subscriber

    init_db(db_path)
    sid = register_subscriber("https://example.com/webhook", "whsec_test", min_score=70, db_path=db_path)
    assert re.match(r"^[0-9a-f-]{36}$", sid)

    subs = list_subscribers(db_path=db_path)
    assert len(subs) == 1
    assert subs[0].subscriber_id == sid
    assert subs[0].url == "https://example.com/webhook"
    assert subs[0].min_score == 70
    assert subs[0].active is True


def test_get_subscriber(db_path):
    from detection.webhook_registry import get_subscriber, init_db, register_subscriber

    init_db(db_path)
    sid = register_subscriber("https://example.com/webhook", "whsec_test", db_path=db_path)
    sub = get_subscriber(sid, db_path)
    assert sub is not None
    assert sub.secret == "whsec_test"
    # verify secret is retrievable (not hashed)
    assert sub.secret != "sha256"


def test_get_subscriber_not_found(db_path):
    from detection.webhook_registry import get_subscriber, init_db

    init_db(db_path)
    assert get_subscriber("nonexistent", db_path) is None


def test_deactivate_subscriber(db_path):
    from detection.webhook_registry import (
        deactivate_subscriber,
        init_db,
        list_subscribers,
        register_subscriber,
    )

    init_db(db_path)
    sid = register_subscriber("https://example.com/webhook", "whsec_test", db_path=db_path)
    assert deactivate_subscriber(sid, db_path) is True
    assert len(list_subscribers(db_path=db_path)) == 0
    # second call returns False
    assert deactivate_subscriber(sid, db_path) is False


def test_list_subscribers_inactive_included(db_path):
    from detection.webhook_registry import (
        deactivate_subscriber,
        init_db,
        list_subscribers,
        register_subscriber,
    )

    init_db(db_path)
    sid = register_subscriber("https://example.com/webhook", "whsec_test", db_path=db_path)
    deactivate_subscriber(sid, db_path)
    assert len(list_subscribers(active_only=False, db_path=db_path)) == 1


# ---------------------------------------------------------------------------
# Encryption / secret handling
# ---------------------------------------------------------------------------


def test_secret_encrypt_decrypt_roundtrip(db_path):
    from detection.webhook_registry import init_db, list_subscribers, register_subscriber

    init_db(db_path)
    register_subscriber("https://example.com/webhook", "my_super_secret_key_123!", db_path=db_path)
    sub = list_subscribers(db_path=db_path)[0]
    # the secret must be retrievable in plaintext (HMAC needs it)
    assert sub.secret == "my_super_secret_key_123!"


def test_secret_not_hashed(db_path):
    from detection.webhook_registry import init_db, register_subscriber, _connect

    init_db(db_path)
    register_subscriber("https://example.com/webhook", "whsec_test", db_path=db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT secret_encrypted FROM webhook_subscribers").fetchone()
    encrypted = row[0]
    # should be base64 of nonce+ciphertext,  not a SHA-256 hex digest
    assert len(encrypted) > 44
    assert encrypted != "sha256=xxx"


def test_masked_secret(db_path):
    from detection.webhook_registry import init_db, list_subscribers, register_subscriber

    init_db(db_path)
    register_subscriber("https://example.com/webhook", "sk_live_abcdefghijklmnop", db_path=db_path)
    sub = list_subscribers(db_path=db_path)[0]
    masked = sub.masked_secret()
    assert "****" in masked
    assert "abcdefghijklmnop" not in masked
    assert sub.masked_secret() != sub.secret  # masked must differ from plaintext


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


def test_ssrf_rejects_http(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="scheme must be https"):
        register_subscriber("http://evil.com/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_localhost(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="Localhost"):
        register_subscriber("https://localhost:8000/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_private_ip_10(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber("https://10.0.0.1/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_private_ip_192_168(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber("https://192.168.1.1/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_private_ip_172(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="Private IP"):
        register_subscriber("https://172.16.0.1/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_reserved_ip_127(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="Localhost"):
        register_subscriber("https://127.0.0.1/webhook", "whsec_test", db_path=db_path)


def test_ssrf_rejects_unresolvable_hostname(db_path):
    from detection.webhook_registry import init_db, register_subscriber

    init_db(db_path)
    with pytest.raises(ValueError, match="could not be resolved"):
        register_subscriber(
            "https://thishostnamedoesnotexistzzzzzzzzzz.com/webhook",
            "whsec_test",
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_get_matching_respects_min_score(db_path):
    from detection.webhook_registry import get_matching_subscribers, init_db, register_subscriber

    init_db(db_path)
    register_subscriber("https://example.com/webhook", "whsec_test", min_score=70, db_path=db_path)
    register_subscriber("https://other.com/webhook", "whsec_other", min_score=90, db_path=db_path)

    low = _score(score=50)
    assert len(get_matching_subscribers(low, db_path)) == 0

    mid = _score(score=80)
    assert len(get_matching_subscribers(mid, db_path)) == 1

    high = _score(score=95)
    assert len(get_matching_subscribers(high, db_path)) == 2


def test_get_matching_respects_wallet_filter(db_path):
    from detection.webhook_registry import get_matching_subscribers, init_db, register_subscriber

    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook",
        "whsec_test",
        min_score=50,
        wallet_filter="GABC,GDEF",
        db_path=db_path,
    )

    match = _score(wallet="GABC", score=60)
    assert len(get_matching_subscribers(match, db_path)) == 1

    no_match = _score(wallet="GXYZ", score=60)
    assert len(get_matching_subscribers(no_match, db_path)) == 0


def test_get_matching_respects_asset_pair_filter(db_path):
    from detection.webhook_registry import get_matching_subscribers, init_db, register_subscriber

    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook",
        "whsec_test",
        min_score=50,
        asset_pair_filter="XLM/USDC",
        db_path=db_path,
    )

    match = _score(asset_pair="XLM/USDC", score=60)
    assert len(get_matching_subscribers(match, db_path)) == 1

    no_match = _score(asset_pair="BTC/USDC", score=60)
    assert len(get_matching_subscribers(no_match, db_path)) == 0


def test_get_matching_wallet_filter_is_null_all_wallets(db_path):
    from detection.webhook_registry import get_matching_subscribers, init_db, register_subscriber

    init_db(db_path)
    register_subscriber(
        "https://example.com/webhook", "whsec_test", min_score=50, db_path=db_path
    )

    result = get_matching_subscribers(_score(wallet="GXYZ", score=60), db_path)
    assert len(result) == 1  # no filter = matches any wallet


# ---------------------------------------------------------------------------
# URL validation unit
# ---------------------------------------------------------------------------


def test_validate_webhook_url_accepts_valid():
    from detection.webhook_registry import validate_webhook_url

    validate_webhook_url("https://example.com/webhook")
    validate_webhook_url("https://httpbin.org/post")


def test_validate_webhook_url_rejects_no_hostname():
    from detection.webhook_registry import validate_webhook_url

    with pytest.raises(ValueError, match="hostname"):
        validate_webhook_url("https:///path")
