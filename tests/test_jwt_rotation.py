"""Tests for short-lived, audience-scoped JWTs with refresh-token rotation (#967)."""

from __future__ import annotations

import time

import pytest

from api import policy
from api.auth import TokenError, TokenService


@pytest.fixture
def svc():
    return TokenService(signing_key="test-signing-key", access_ttl=300, refresh_ttl=3600)


@pytest.fixture
def alerts():
    captured: list[tuple[str, dict]] = []

    def hook(event, details):
        captured.append((event, details))

    policy.SECURITY_ALERT_HOOKS.append(hook)
    yield captured
    policy.SECURITY_ALERT_HOOKS.remove(hook)


def test_access_token_is_short_lived(svc):
    tokens = svc.issue("user-1", ["rest"], "read:scores", audience="rest")
    claims = svc.verify_access(tokens["access_token"], "rest")
    assert tokens["expires_in"] == 300
    assert claims["exp"] - claims["iat"] == 300
    assert claims["sub"] == "user-1"


def test_expired_access_token_rejected():
    svc = TokenService(signing_key="k", access_ttl=1)
    token = svc.issue("u", ["rest"], "read:scores", audience="rest")["access_token"]
    time.sleep(1.1)
    with pytest.raises(TokenError, match="expired"):
        svc.verify_access(token, "rest")


def test_access_token_is_audience_scoped(svc):
    token = svc.issue("u", ["rest", "graphql"], "read:scores", audience="rest")["access_token"]
    assert svc.verify_access(token, "rest")["aud"] == "rest"
    with pytest.raises(TokenError, match="audience"):
        svc.verify_access(token, "graphql")


def test_issue_rejects_unpermitted_audience(svc):
    with pytest.raises(TokenError):
        svc.issue("u", ["rest"], "read:scores", audience="grpc")


def test_tampered_token_rejected(svc):
    token = svc.issue("u", ["rest"], "read:scores", audience="rest")["access_token"]
    header, payload, sig = token.split(".")
    with pytest.raises(TokenError):
        svc.verify_access(f"{header}.{payload}x.{sig}", "rest")
    with pytest.raises(TokenError):
        TokenService(signing_key="other-key").verify_access(token, "rest")


def test_refresh_token_cannot_be_used_as_access(svc):
    tokens = svc.issue("u", ["rest"], "read:scores", audience="rest")
    with pytest.raises(TokenError):
        svc.verify_access(tokens["refresh_token"], "rest")


def test_refresh_rotates_token(svc, alerts):
    first = svc.issue("u", ["rest", "ws"], "read:scores", audience="rest")
    second = svc.refresh(first["refresh_token"], audience="ws")
    assert second["refresh_token"] != first["refresh_token"]
    assert svc.verify_access(second["access_token"], "ws")["aud"] == "ws"
    third = svc.refresh(second["refresh_token"], audience="rest")
    assert svc.verify_access(third["access_token"], "rest")
    assert alerts == []


def test_refresh_reuse_invalidates_family_and_alerts(svc, alerts):
    first = svc.issue("u", ["rest"], "read:scores", audience="rest")
    second = svc.refresh(first["refresh_token"], audience="rest")

    # Attacker replays the rotated-out token.
    with pytest.raises(TokenError, match="reuse"):
        svc.refresh(first["refresh_token"], audience="rest")

    # Whole family is dead: the legitimate client's newer tokens stop working too.
    with pytest.raises(TokenError, match="revoked"):
        svc.refresh(second["refresh_token"], audience="rest")
    with pytest.raises(TokenError, match="revoked"):
        svc.verify_access(second["access_token"], "rest")
    with pytest.raises(TokenError, match="revoked"):
        svc.verify_access(first["access_token"], "rest")

    assert len(alerts) == 1
    event, details = alerts[0]
    assert event == "refresh_token_reuse"
    assert details["subject"] == "u"


def test_reuse_does_not_affect_other_families(svc, alerts):
    a = svc.issue("u", ["rest"], "read:scores", audience="rest")
    b = svc.issue("u", ["rest"], "read:scores", audience="rest")
    svc.refresh(a["refresh_token"], audience="rest")
    with pytest.raises(TokenError):
        svc.refresh(a["refresh_token"], audience="rest")
    assert svc.verify_access(b["access_token"], "rest")
    assert svc.refresh(b["refresh_token"], audience="rest")


def test_refresh_rejects_audience_outside_family(svc):
    tokens = svc.issue("u", ["rest"], "read:scores", audience="rest")
    with pytest.raises(TokenError, match="audience"):
        svc.refresh(tokens["refresh_token"], audience="grpc")
