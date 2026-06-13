import importlib

import config.settings as settings_module


def test_defaults_when_env_unset(monkeypatch):
    for key in (
        "HORIZON_URL",
        "BENFORD_MAD_THRESHOLD",
        "RISK_SCORE_THRESHOLD",
        "MODEL_DIR",
        "LEDGERLENS_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = importlib.reload(settings_module).settings

    assert settings.horizon_url == "https://horizon.stellar.org"
    assert settings.benford_mad_threshold == 0.015
    assert settings.risk_score_threshold == 70
    assert settings.model_dir == "./models"
    assert settings.db_path == "./ledgerlens.db"


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "85")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", "/tmp/custom.db")

    settings = importlib.reload(settings_module).settings

    assert settings.risk_score_threshold == 85
    assert settings.db_path == "/tmp/custom.db"

    importlib.reload(settings_module)
