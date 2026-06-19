import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from detection.risk_score import RiskScore
from detection.storage import save_rings, save_scores


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


def _score(
    wallet,
    asset_pair,
    score,
    *,
    benford_flag=None,
    ml_flag=None,
    confidence=90,
    timestamp=None,
) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50 if benford_flag is None else benford_flag,
        ml_flag=score > 50 if ml_flag is None else ml_flag,
        confidence=confidence,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def test_health(client, tmp_path, monkeypatch):
    """Healthy path: DB reachable and all model stub files present → 200 all-ok."""
    import config.settings as settings_module
    from detection.model_inference import _MODEL_FILENAMES

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    for filename in _MODEL_FILENAMES.values():
        (model_dir / filename).write_bytes(b"stub")

    object.__setattr__(settings_module.settings, "model_dir", str(model_dir))

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["models"] == "ok"



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


def test_list_scores_filters_by_benford_flag(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("GBENFORD", "XLM/USDC", 60, benford_flag=True, ml_flag=False),
            _score("GCLEAN", "XLM/USDC", 95, benford_flag=False, ml_flag=True),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?benford_flag=true")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["GBENFORD"]


def test_list_scores_filters_by_ml_flag_false(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("GML", "XLM/USDC", 95, benford_flag=False, ml_flag=True),
            _score("GNO_ML", "XLM/USDC", 60, benford_flag=True, ml_flag=False),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?ml_flag=false")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["GNO_ML"]


def test_list_scores_combines_flag_filters_and_min_score(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("GMATCH", "XLM/USDC", 80, benford_flag=True, ml_flag=False),
            _score("GLOW", "XLM/USDC", 40, benford_flag=True, ml_flag=False),
            _score("GWRONG_FLAG", "XLM/USDC", 95, benford_flag=True, ml_flag=True),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?min_score=50&benford_flag=true&ml_flag=false")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["GMATCH"]


def test_list_scores_sorts_by_confidence(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("GLOW_CONF", "XLM/USDC", 95, confidence=20),
            _score("GHIGH_CONF", "XLM/USDC", 80, confidence=99),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?sort_by=confidence")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["GHIGH_CONF", "GLOW_CONF"]


def test_list_scores_sorts_by_timestamp(client):
    import detection.storage as storage_module

    now = datetime.now(timezone.utc)
    save_scores(
        [
            _score("GOLDER", "XLM/USDC", 95, timestamp=now - timedelta(minutes=10)),
            _score("GNEWER", "XLM/USDC", 80, timestamp=now),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?sort_by=timestamp")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["GNEWER", "GOLDER"]


def test_list_scores_rejects_invalid_sort_by(client):
    response = client.get("/scores?sort_by=invalid")
    assert response.status_code == 422


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
    assert len(body["subscriber_id"]) == 36


def test_create_webhook_rejects_http(client):
    response = client.post(
        "/webhooks",
        json={"url": "http://evil.com/webhook", "secret": "whsec_test"},
    )
    assert response.status_code == 422


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


def test_list_scores_accepts_limit_offset(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("W1", "XLM/USDC", 10),
            _score("W2", "XLM/USDC", 20),
            _score("W3", "XLM/USDC", 30),
        ],
        storage_module.settings.db_path,
    )

    resp = client.get("/scores?limit=2&offset=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [row["wallet"] for row in body] == ["W2", "W1"]


def test_alerts_accepts_limit_offset(client):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "risk_score_threshold", 0)

    save_scores(
        [
            _score("W1", "XLM/USDC", 10),
            _score("W2", "XLM/USDC", 20),
            _score("W3", "XLM/USDC", 30),
        ],
        storage_module.settings.db_path,
    )

    resp = client.get("/alerts?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [row["wallet"] for row in body] == ["W3", "W2"]


def test_limit_offset_out_of_range_returns_422(client):
    resp = client.get("/scores?limit=0&offset=0")
    assert resp.status_code == 422

    resp = client.get("/scores?limit=1001&offset=0")
    assert resp.status_code == 422

    resp = client.get("/scores?limit=10&offset=-1")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /correlations
# ---------------------------------------------------------------------------


def test_correlations_empty(client):
    resp = client.get("/correlations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_correlations_returns_stored_data(client, monkeypatch):
    import detection.storage as storage_module

    storage_module.save_pair_correlations(
        [("XLM/USDC", "XLM/AQUA", 0.88)],
        method="spearman",
        shared_wallet_counts={("XLM/USDC", "XLM/AQUA"): 3},
        db_path=storage_module.settings.db_path,
    )

    resp = client.get("/correlations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["pair_a"] == "XLM/USDC"
    assert row["pair_b"] == "XLM/AQUA"
    assert abs(row["correlation_r"] - 0.88) < 1e-6
    assert row["method"] == "spearman"
    assert row["shared_wallet_count"] == 3


def test_correlations_returns_only_latest_run(client, monkeypatch):
    import time as _time

    import detection.storage as storage_module

    db = storage_module.settings.db_path

    storage_module.save_pair_correlations(
        [("XLM/USDC", "XLM/AQUA", 0.80)],
        method="spearman",
        db_path=db,
    )
    _time.sleep(0.01)
    storage_module.save_pair_correlations(
        [("XLM/USDC", "XLM/yXLM", 0.91)],
        method="spearman",
        db_path=db,
    )

    resp = client.get("/correlations")
    body = resp.json()
    pairs = {(r["pair_a"], r["pair_b"]) for r in body}
    assert ("XLM/USDC", "XLM/yXLM") in pairs
    assert ("XLM/USDC", "XLM/AQUA") not in pairs


# ---------------------------------------------------------------------------
# /rings
# ---------------------------------------------------------------------------


def test_rings_empty(client):
    resp = client.get("/rings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_rings_returns_stored_data(client):
    import detection.storage as storage_module

    storage_module.save_rings(
        [
            {
                "accounts": ["A", "B", "C"],
                "total_volume": 300.0,
                "cycle_volume": 100.0,
                "avg_trade_count": 1.0,
                "timing_tightness": 0.0,
                "truncated": False,
            }
        ],
        db_path=storage_module.settings.db_path,
    )

    resp = client.get("/rings")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["accounts"] == ["A", "B", "C"]
    assert row["total_volume"] == 300.0
    assert row["cycle_volume"] == 100.0
    assert row["detected_at"]
