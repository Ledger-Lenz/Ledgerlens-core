import base64
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detection.risk_score import RiskScore
from detection.storage import save_scores


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", db_path)

    from api.main import app

    return TestClient(app)


def _score(wallet, asset_pair, score) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_scores_empty(client):
    response = client.get("/scores")
    assert response.status_code == 200
    assert response.json() == []


def test_list_scores_and_filter_by_min_score(client, monkeypatch):
    from api.main import app  # noqa: F401
    import detection.storage as storage_module

    save_scores([_score("GABC", "XLM/USDC", 80), _score("GXYZ", "XLM/USDC", 20)], storage_module.settings.db_path)

    response = client.get("/scores")
    assert response.status_code == 200
    assert len(response.json()) == 2

    response = client.get("/scores?min_score=50")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "GABC"


def test_wallet_scores_not_found(client):
    response = client.get("/scores/GABC")
    assert response.status_code == 404


def test_wallet_scores_found(client):
    import detection.storage as storage_module

    save_scores([_score("GABC", "XLM/USDC", 80)], storage_module.settings.db_path)

    response = client.get("/scores/GABC")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "GABC"


def test_alerts_filters_by_threshold(client):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "risk_score_threshold", 70)

    save_scores([_score("GABC", "XLM/USDC", 80), _score("GXYZ", "XLM/USDC", 20)], storage_module.settings.db_path)

    response = client.get("/alerts")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "GABC"


def test_asset_risk_ranking(client):
    import detection.storage as storage_module

    save_scores(
        [_score("GABC", "XLM/USDC", 80), _score("GXYZ", "XLM/USDC", 40), _score("GDEF", "BTC/USDC", 10)],
        storage_module.settings.db_path,
    )

    response = client.get("/assets/risk-ranking")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["asset_pair"] == "XLM/USDC"
    assert body[0]["average_score"] == 60.0
    assert body[0]["wallet_count"] == 2


# ---------------------------------------------------------------------------
# Webhook subscriber management API
# ---------------------------------------------------------------------------


def test_create_webhook(client):
    response = client.post(
        "/webhooks",
        json={"url": "https://example.com/webhook", "secret": "whsec_test", "min_score": 70},
    )
    assert response.status_code == 201
    body = response.json()
    assert "subscriber_id" in body
    assert len(body["subscriber_id"]) == 36  # UUID


def test_create_webhook_rejects_http(client):
    response = client.post(
        "/webhooks",
        json={"url": "http://evil.com/webhook", "secret": "whsec_test"},
    )
    assert response.status_code == 422  # Validation error


def test_list_webhooks(client):
    client.post(
        "/webhooks",
        json={"url": "https://example.com/webhook", "secret": "whsec_test"},
    )
    response = client.get("/webhooks")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["url"] == "https://example.com/webhook"
    # secret must be masked
    assert "****" in body[0]["secret"]
    assert "whsec_test" not in body[0]["secret"]


def test_list_webhooks_empty(client):
    response = client.get("/webhooks")
    assert response.status_code == 200
    assert response.json() == []


def test_delete_webhook(client):
    resp = client.post(
        "/webhooks",
        json={"url": "https://example.com/webhook", "secret": "whsec_test"},
    )
    sid = resp.json()["subscriber_id"]

    response = client.delete(f"/webhooks/{sid}")
    assert response.status_code == 200
    assert response.json() == {"status": "deactivated"}

    # no longer in active list
    assert len(client.get("/webhooks").json()) == 0


def test_delete_webhook_not_found(client):
    response = client.delete("/webhooks/nonexistent")
    assert response.status_code == 404


def test_dead_letters_endpoint(client):
    response = client.get("/webhooks/dead-letters")
    assert response.status_code == 200
    assert response.json() == []


def test_create_webhook_with_filters(client):
    response = client.post(
        "/webhooks",
        json={
            "url": "https://example.com/webhook",
            "secret": "whsec_test",
            "min_score": 80,
            "wallet_filter": "GABC,GDEF",
            "asset_pair_filter": "XLM/USDC",
        },
    )
    assert response.status_code == 201

    body = client.get("/webhooks").json()
    assert len(body) == 1
    assert body[0]["wallet_filter"] == "GABC,GDEF"
    assert body[0]["asset_pair_filter"] == "XLM/USDC"
    assert body[0]["min_score"] == 80
