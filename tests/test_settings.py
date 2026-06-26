import importlib

import pytest

import config.settings as settings_module


def test_defaults_when_env_unset(monkeypatch):
    for key in (
        "HORIZON_URL",
        "BENFORD_MAD_THRESHOLD",
        "RISK_SCORE_THRESHOLD",
        "MODEL_DIR",
        "LEDGERLENS_DB_PATH",
        "ENSEMBLE_WEIGHT_RF",
        "ENSEMBLE_WEIGHT_XGB",
        "ENSEMBLE_WEIGHT_LGBM",
        "STREAMER_QUEUE_MAXSIZE",
        "STREAMER_OVERFLOW_STRATEGY",
        "STREAMER_HIGH_WATER_RATIO",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = importlib.reload(settings_module).settings

    assert settings.horizon_url == "https://horizon.stellar.org"
    assert settings.benford_mad_threshold == 0.015
    assert settings.risk_score_threshold == 70
    assert settings.model_dir == "./models"
    assert settings.db_path == "./ledgerlens.db"
    assert settings.ensemble_weight_rf == 0.25
    assert settings.ensemble_weight_xgb == 0.50
    assert settings.ensemble_weight_lgbm == 0.25
    assert settings.streamer_queue_maxsize == 1000
    assert settings.streamer_overflow_strategy == "drop_oldest"
    assert settings.streamer_high_water_ratio == 0.8


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "85")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", "/tmp/custom.db")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_RF", "2")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_XGB", "3")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_LGBM", "5")

    settings = importlib.reload(settings_module).settings

    assert settings.risk_score_threshold == 85
    assert settings.db_path == "/tmp/custom.db"
    assert settings.ensemble_weight_rf == 2
    assert settings.ensemble_weight_xgb == 3
    assert settings.ensemble_weight_lgbm == 5

    importlib.reload(settings_module)


def test_negative_ensemble_weight_raises(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_WEIGHT_RF", "-0.01")

    with pytest.raises(ValueError, match="Ensemble weights must be non-negative"):
        settings_module.Settings()


def test_all_zero_ensemble_weights_raise(monkeypatch):
    monkeypatch.setenv("ENSEMBLE_WEIGHT_RF", "0")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_XGB", "0")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_LGBM", "0")

    with pytest.raises(ValueError, match="At least one ensemble weight must be positive"):
        settings_module.Settings()


def test_cors_wildcard_origin_raises(monkeypatch):
    monkeypatch.setenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValueError, match="must not contain '\\*'"):
        settings_module.Settings()


def test_cors_wildcard_in_list_raises(monkeypatch):
    monkeypatch.setenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", "https://ok.example.com,*")

    with pytest.raises(ValueError, match="must not contain '\\*'"):
        settings_module.Settings()


def test_cors_default_is_empty_tuple(monkeypatch):
    monkeypatch.delenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", raising=False)

    settings = settings_module.Settings()

    assert settings.cors_allowed_origins == ()
