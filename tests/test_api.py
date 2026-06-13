from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detection.risk_score import RiskScore
from detection.storage import save_scores


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
