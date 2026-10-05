"""Tests for API-key-tier-aware adaptive rate limiting in the WAF (#968)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import policy
from api.waf_middleware import WAFMiddleware
from config.settings import settings
from detection.rate_limiter import reset_rate_limiter


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated key DB, in-process rate limiter, WAF tier limiting enabled."""
    monkeypatch.setattr(settings, "ledgerlens_db_path", str(tmp_path / "keys.db"))
    monkeypatch.setattr(settings, "gateway_quota_store", "sqlite")
    monkeypatch.setattr(settings, "waf_tier_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "waf_enabled", True)
    monkeypatch.delenv(policy.TIER_LIMITS_FILE_ENV, raising=False)
    reset_rate_limiter()
    policy.set_tier_limits({})
    policy.key_cycling_detector.reset()
    yield
    policy.set_tier_limits({})
    policy.key_cycling_detector.reset()
    reset_rate_limiter()


@pytest.fixture
def client(env):
    app = FastAPI()
    app.add_middleware(WAFMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def _key(tier: str) -> str:
    from detection.api_key_store import create_api_key

    return create_api_key(scopes=["read:scores"], tier=tier)["plaintext_key"]


def _burst(client: TestClient, n: int, headers: dict | None = None) -> list[int]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda _: client.get("/ping", headers=headers or {}).status_code, range(n)))


def test_anonymous_traffic_limited_by_ip(client):
    policy.set_tier_limits({"anonymous": {"requests_per_minute": 5}})
    codes = _burst(client, 12)
    assert codes.count(200) == 5
    assert codes.count(429) == 7


def test_limits_enforced_per_tier_under_concurrent_load(client):
    policy.set_tier_limits({
        "free": {"requests_per_minute": 4},
        "enterprise": {"requests_per_minute": 20},
    })
    free, enterprise = _key("free"), _key("enterprise")

    free_codes = _burst(client, 15, {"X-LedgerLens-Api-Key": free})
    ent_codes = _burst(client, 30, {"X-LedgerLens-Api-Key": enterprise})

    assert free_codes.count(200) == 4
    assert ent_codes.count(200) == 20


def test_keys_have_independent_buckets(client):
    policy.set_tier_limits({"free": {"requests_per_minute": 3}})
    a, b = _key("free"), _key("free")
    assert _burst(client, 5, {"X-LedgerLens-Api-Key": a}).count(200) == 3
    assert _burst(client, 5, {"X-LedgerLens-Api-Key": b}).count(200) == 3


def test_429_carries_retry_after_and_tier(client):
    policy.set_tier_limits({"free": {"requests_per_minute": 1}})
    headers = {"X-LedgerLens-Api-Key": _key("free")}
    client.get("/ping", headers=headers)
    resp = client.get("/ping", headers=headers)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-LedgerLens-Tier"] == "free"


def test_invalid_key_falls_back_to_ip_bucket(client):
    policy.set_tier_limits({"anonymous": {"requests_per_minute": 2}})
    codes = _burst(client, 4, {"X-LedgerLens-Api-Key": "ll_bogus"})
    assert codes.count(200) == 2


def test_tier_limits_reloaded_from_file_at_runtime(tmp_path, monkeypatch, env):
    path = tmp_path / "tiers.json"
    path.write_text(json.dumps({"standard": {"requests_per_minute": 7}}))
    monkeypatch.setenv(policy.TIER_LIMITS_FILE_ENV, str(path))
    assert policy.get_tier_limits("standard")["requests_per_minute"] == 7

    path.write_text(json.dumps({"standard": {"requests_per_minute": 9}}))
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert policy.get_tier_limits("standard")["requests_per_minute"] == 9

    # Malformed update keeps last good value.
    path.write_text("{not json")
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    assert policy.get_tier_limits("standard")["requests_per_minute"] == 9


def test_runtime_overrides_take_precedence(env):
    policy.set_tier_limits({"free": {"requests_per_minute": 1}})
    assert policy.get_tier_limits("free")["requests_per_minute"] == 1
    policy.set_tier_limits({})
    assert (
        policy.get_tier_limits("free")["requests_per_minute"]
        == policy.DEFAULT_TIER_LIMITS["free"]["requests_per_minute"]
    )


def test_key_cycling_detected_and_alerted(client, monkeypatch):
    monkeypatch.setattr(settings, "waf_key_cycling_threshold", 5)
    alerts: list = []

    def hook(event, details):
        alerts.append((event, details))

    policy.SECURITY_ALERT_HOOKS.append(hook)
    try:
        for i in range(8):
            client.get("/ping", headers={"X-LedgerLens-Api-Key": f"ll_stolen_{i}"})
    finally:
        policy.SECURITY_ALERT_HOOKS.remove(hook)

    cycling = [d for e, d in alerts if e == "api_key_cycling"]
    assert len(cycling) == 1  # alert is de-duplicated within the window
    assert cycling[0]["distinct_keys"] >= 5


def test_key_cycling_detector_groups_by_ip_range():
    detector = policy.KeyCyclingDetector(threshold=3, window_seconds=60)
    assert not detector.observe("10.0.0.1", "k1", now=0)
    assert not detector.observe("10.0.0.2", "k2", now=1)
    assert detector.observe("10.0.0.3", "k3", now=2)
    # Different /24 is tracked separately; expired entries age out.
    assert not detector.observe("10.0.1.1", "k4", now=3)
    assert not detector.observe("10.0.0.4", "k5", now=200)


def test_disabled_setting_bypasses_limit(client, monkeypatch):
    policy.set_tier_limits({"anonymous": {"requests_per_minute": 1}})
    monkeypatch.setattr(settings, "waf_tier_rate_limit_enabled", False)
    assert _burst(client, 5).count(200) == 5
