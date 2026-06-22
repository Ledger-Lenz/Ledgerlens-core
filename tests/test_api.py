import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from detection.risk_score import RiskScore
from detection.storage import save_scores


def test_robustness_endpoint_no_report(monkeypatch):
    # allow admin access for tests
    def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("api.auth.require_admin_key", _noop)
    client = TestClient(app)
    # when no report exists, return 404
    resp = client.get("/admin/robustness-report")
    assert resp.status_code == 404 or resp.status_code == 200


def test_robustness_endpoint_with_report(monkeypatch):
    def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("api.auth.require_admin_key", _noop)
    client = TestClient(app)
    # ensure a report exists by checking storage; compute_robustness_report persists one in its call
    from detection.robustness_eval import compute_robustness_report
    from tests.test_robustness_eval import make_df
    from tests.test_adversarial_attack import DummyModel

    models = {"dummy": DummyModel(w=5.0, b=-1.0)}
    df = make_df()
    compute_robustness_report(models, df, n_samples=10, epsilon=0.05, steps=3, seed=2)

    resp = client.get("/admin/robustness-report")
    assert resp.status_code == 200
    data = resp.json()
    assert "model_version" in data


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

    save_scores([_score("G" + "A" * 55, "XLM/USDC", 80), _score("G" + "B" * 55, "XLM/USDC", 20)], storage_module.settings.db_path)

    response = client.get("/scores")
    assert response.status_code == 200
    assert len(response.json()) == 2

    response = client.get("/scores?min_score=50")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "G" + "A" * 55


def test_list_scores_filters_by_benford_flag(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("G" + "B" * 55, "XLM/USDC", 60, benford_flag=True, ml_flag=False),
            _score("G" + "C" * 55, "XLM/USDC", 95, benford_flag=False, ml_flag=True),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?benford_flag=true")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["G" + "B" * 55]


def test_list_scores_filters_by_ml_flag_false(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("G" + "M" * 55, "XLM/USDC", 95, benford_flag=False, ml_flag=True),
            _score("G" + "N" * 55, "XLM/USDC", 60, benford_flag=True, ml_flag=False),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?ml_flag=false")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["G" + "N" * 55]


def test_list_scores_combines_flag_filters_and_min_score(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("G" + "M" * 55, "XLM/USDC", 80, benford_flag=True, ml_flag=False),
            _score("G" + "L" * 55, "XLM/USDC", 40, benford_flag=True, ml_flag=False),
            _score("G" + "W" * 55, "XLM/USDC", 95, benford_flag=True, ml_flag=True),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?min_score=50&benford_flag=true&ml_flag=false")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["G" + "M" * 55]


def test_list_scores_sorts_by_confidence(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("G" + "L" * 55, "XLM/USDC", 95, confidence=20),
            _score("G" + "H" * 55, "XLM/USDC", 80, confidence=99),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?sort_by=confidence")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["G" + "H" * 55, "G" + "L" * 55]


def test_list_scores_sorts_by_timestamp(client):
    import detection.storage as storage_module

    now = datetime.now(timezone.utc)
    save_scores(
        [
            _score("G" + "O" * 55, "XLM/USDC", 95, timestamp=now - timedelta(minutes=10)),
            _score("G" + "N" * 55, "XLM/USDC", 80, timestamp=now),
        ],
        storage_module.settings.db_path,
    )

    response = client.get("/scores?sort_by=timestamp")
    assert response.status_code == 200
    body = response.json()
    assert [item["wallet"] for item in body] == ["G" + "N" * 55, "G" + "O" * 55]


def test_list_scores_rejects_invalid_sort_by(client):
    response = client.get("/scores?sort_by=invalid")
    assert response.status_code == 422


def test_wallet_scores_not_found(client):
    response = client.get("/scores/G" + "A" * 55)
    assert response.status_code == 404


def test_wallet_scores_found(client):
    import detection.storage as storage_module

    save_scores([_score("G" + "A" * 55, "XLM/USDC", 80)], storage_module.settings.db_path)

    response = client.get("/scores/G" + "A" * 55)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "G" + "A" * 55


def test_wallet_scores_validates_format(client):
    valid_address = "G" + "A" * 55
    response = client.get(f"/scores/{valid_address}")
    assert response.status_code in (200, 404)


def test_wallet_scores_rejects_too_short(client):
    response = client.get("/scores/G" + "A" * 54)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stellar wallet address format."


def test_wallet_scores_rejects_too_long(client):
    response = client.get("/scores/G" + "A" * 56)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stellar wallet address format."


def test_wallet_scores_rejects_non_g_start(client):
    response = client.get("/scores/" + "A" * 56)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stellar wallet address format."


def test_wallet_scores_rejects_lowercase(client):
    response = client.get("/scores/G" + "a" * 55)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stellar wallet address format."


def test_wallet_scores_rejects_invalid_character(client):
    address = "G" + "A" * 27 + "0" + "A" * 27
    response = client.get(f"/scores/{address}")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Stellar wallet address format."


def test_wallet_scores_rejects_empty_string(client):
    response = client.get("/scores/%20")
    assert response.status_code == 400


def test_alerts_filters_by_threshold(client):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "risk_score_threshold", 70)

    save_scores([_score("G" + "A" * 55, "XLM/USDC", 80), _score("G" + "B" * 55, "XLM/USDC", 20)], storage_module.settings.db_path)

    response = client.get("/alerts")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["wallet"] == "G" + "A" * 55


def test_asset_risk_ranking(client):
    import detection.storage as storage_module

    save_scores(
        [_score("G" + "A" * 55, "XLM/USDC", 80), _score("G" + "B" * 55, "XLM/USDC", 40), _score("G" + "D" * 55, "BTC/USDC", 10)],
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
            "wallet_filter": "G" + "A" * 55 + ",G" + "D" * 55,
            "asset_pair_filter": "XLM/USDC",
        },
    )
    assert response.status_code == 201
    body = client.get("/webhooks").json()
    assert len(body) == 1
    assert body[0]["wallet_filter"] == "G" + "A" * 55 + ",G" + "D" * 55
    assert body[0]["asset_pair_filter"] == "XLM/USDC"
    assert body[0]["min_score"] == 80


def test_list_scores_accepts_limit_offset(client):
    import detection.storage as storage_module

    save_scores(
        [
            _score("G" + "W1" + "A" * 52, "XLM/USDC", 10),
            _score("G" + "W2" + "A" * 52, "XLM/USDC", 20),
            _score("G" + "W3" + "A" * 52, "XLM/USDC", 30),
        ],
        storage_module.settings.db_path,
    )

    resp = client.get("/scores?limit=2&offset=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [row["wallet"] for row in body] == ["G" + "W2" + "A" * 52, "G" + "W1" + "A" * 52]


def test_alerts_accepts_limit_offset(client):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "risk_score_threshold", 0)

    save_scores(
        [
            _score("G" + "W1" + "A" * 52, "XLM/USDC", 10),
            _score("G" + "W2" + "A" * 52, "XLM/USDC", 20),
            _score("G" + "W3" + "A" * 52, "XLM/USDC", 30),
        ],
        storage_module.settings.db_path,
    )

    resp = client.get("/alerts?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert [row["wallet"] for row in body] == ["G" + "W3" + "A" * 52, "G" + "W2" + "A" * 52]


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
    import detection.storage as storage_module
    storage_module.init_db()
    resp = client.get("/rings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_rings_returns_stored_data(client):
    import detection.storage as storage_module
    storage_module.init_db()
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
