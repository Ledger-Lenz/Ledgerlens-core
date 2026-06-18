import base64
import os
from datetime import datetime, timedelta, timezone

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
# /scores/{wallet}/explain endpoint
# ---------------------------------------------------------------------------


def _seed_shap(db_path: str, wallet: str = "GABC", asset_pair: str = "XLM/USDC") -> list[dict]:
    """Write a feature vector + SHAP cache and return the shap payload."""
    from detection.storage import save_feature_vectors, save_shap_values

    features = {"benford_mad_24h": 0.02, "round_trip_trade_frequency": 0.1}
    save_feature_vectors([{"wallet": wallet, "asset_pair": asset_pair, "features": features}], db_path)

    shap_payload = [
        {"feature": "benford_mad_24h", "shap_value": 0.40},
        {"feature": "round_trip_trade_frequency", "shap_value": -0.25},
        {"feature": "network_centrality", "shap_value": 0.15},
        {"feature": "off_hours_activity_ratio", "shap_value": -0.08},
        {"feature": "volume_spike_frequency", "shap_value": 0.05},
    ]
    save_shap_values(wallet, asset_pair, shap_payload, db_path)
    return shap_payload


@pytest.fixture
def client_with_models(tmp_path, monkeypatch):
    """TestClient whose lifespan loads a dummy (but non-empty) models dict."""
    import importlib

    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", db_path)

    # Patch load_models so the lifespan succeeds without real model files.
    import detection.model_inference as mi

    monkeypatch.setattr(mi, "load_models", lambda *a, **kw: {"xgboost": object()})

    import api.main as main_module

    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as c:
        yield c, db_path


@pytest.fixture
def client_no_models(tmp_path, monkeypatch):
    """TestClient whose lifespan finds no models (load_models raises FileNotFoundError)."""
    import importlib

    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", db_path)

    import detection.model_inference as mi

    def _raise(*a, **kw):
        raise FileNotFoundError("no models")

    monkeypatch.setattr(mi, "load_models", _raise)

    import api.main as main_module

    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as c:
        yield c, db_path


def test_explain_returns_top5_in_descending_abs_order(client_with_models):
    client, db_path = client_with_models
    _seed_shap(db_path)

    resp = client.get("/scores/GABC/explain?asset_pair=XLM%2FUSDC")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body) == 5
    abs_values = [abs(item["shap_value"]) for item in body]
    assert abs_values == sorted(abs_values, reverse=True), "Items must be ordered by |shap_value| desc"
    assert body[0]["feature"] == "benford_mad_24h"


def test_explain_404_for_unknown_wallet(client_with_models):
    client, _ = client_with_models
    resp = client.get("/scores/GUNKNOWN/explain?asset_pair=XLM%2FUSDC")
    assert resp.status_code == 404


def test_explain_404_when_no_shap_cache(client_with_models):
    """Feature vector exists but no SHAP values have been computed yet."""
    client, db_path = client_with_models
    from detection.storage import save_feature_vectors

    save_feature_vectors(
        [{"wallet": "GABC", "asset_pair": "XLM/USDC", "features": {"benford_mad_24h": 0.02}}],
        db_path,
    )
    resp = client.get("/scores/GABC/explain?asset_pair=XLM%2FUSDC")
    assert resp.status_code == 404


def test_explain_503_when_models_not_loaded(client_no_models):
    client, db_path = client_no_models
    _seed_shap(db_path)
    resp = client.get("/scores/GABC/explain?asset_pair=XLM%2FUSDC")
    assert resp.status_code == 503
    assert "Models not loaded" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /admin/drift-reports and /admin/retrain-runs (admin-key gated)
# ---------------------------------------------------------------------------


def test_drift_reports_503_when_admin_key_not_configured(client):
    resp = client.get("/admin/drift-reports")
    assert resp.status_code == 503


def test_drift_reports_401_when_header_missing(client, monkeypatch):
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "admin_api_key", "secret-key")

    resp = client.get("/admin/drift-reports")
    assert resp.status_code == 401


def test_drift_reports_403_when_header_wrong(client, monkeypatch):
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "admin_api_key", "secret-key")

    resp = client.get("/admin/drift-reports", headers={"X-LedgerLens-Admin-Key": "wrong"})
    assert resp.status_code == 403


def test_drift_reports_returns_saved_reports(client, monkeypatch):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "admin_api_key", "secret-key")

    storage_module.save_drift_report(
        drift_detected=True,
        psi_report={"benford_mad_24h": 0.31},
        psi_threshold=0.20,
        min_drifted_features=3,
        db_path=storage_module.settings.db_path,
    )

    resp = client.get("/admin/drift-reports", headers={"X-LedgerLens-Admin-Key": "secret-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["drift_detected"] is True
    assert body[0]["psi_report"] == {"benford_mad_24h": 0.31}


def test_retrain_runs_returns_saved_runs_filtered_by_model(client, monkeypatch):
    import config.settings as settings_module
    import detection.storage as storage_module

    object.__setattr__(settings_module.settings, "admin_api_key", "secret-key")

    storage_module.save_retrain_run(
        drift_report_id=None,
        model_name="random_forest",
        old_version="aaa11111",
        new_version="bbb22222",
        old_auc_roc=0.91,
        new_auc_roc=0.93,
        promoted=True,
        forced=False,
        db_path=storage_module.settings.db_path,
    )
    storage_module.save_retrain_run(
        drift_report_id=None,
        model_name="xgboost",
        old_version="ccc33333",
        new_version="ddd44444",
        old_auc_roc=0.90,
        new_auc_roc=0.89,
        promoted=False,
        forced=False,
        db_path=storage_module.settings.db_path,
    )

    resp = client.get(
        "/admin/retrain-runs?model_name=random_forest",
        headers={"X-LedgerLens-Admin-Key": "secret-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["model_name"] == "random_forest"
    assert body[0]["promoted"] is True


def test_explain_response_time_under_100ms(client_with_models):
    """Cache-hit response must be served in < 100 ms."""
    import time

    client, db_path = client_with_models
    _seed_shap(db_path)

    start = time.monotonic()
    resp = client.get("/scores/GABC/explain?asset_pair=XLM%2FUSDC")
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 200
    assert elapsed_ms < 100, f"Response took {elapsed_ms:.1f} ms — expected < 100 ms"
