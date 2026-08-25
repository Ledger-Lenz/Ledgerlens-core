"""Tests for config/settings.py — Issue #515.

Isolation guarantees
--------------------
- Every test creates its own ``Settings()`` instance; none share the
  module-level singleton.
- ``env_isolation`` autouse fixture wipes all env vars touched by Settings so
  a stray ``.env`` file or a previously-run test cannot bleed defaults into
  subsequent tests.
- The module-level ``_config_redis_attempted`` flag in ``config.settings`` is
  reset between tests so Redis-connection side-effects from one test do not
  suppress Redis attempts in later tests.
"""

import pytest

import config.settings as settings_module


# ---------------------------------------------------------------------------
# Autouse isolation fixture
# ---------------------------------------------------------------------------

_SETTINGS_ENV_VARS = (
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
    "LEDGERLENS_CORS_ALLOWED_ORIGINS",
    "NETWORK",
)


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch):
    """Remove all env vars that Settings reads so each test starts clean.

    Also resets the module-level Redis-connection state so that the lazy
    ``_get_config_redis_client()`` call in ``config.settings`` does not skip
    connection attempts due to a flag set by a previous test.
    """
    for key in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    # Reset global Redis connection state between tests to prevent hidden
    # side-effects: a failed Redis probe in one test would otherwise suppress
    # all subsequent probe attempts within the same session.
    monkeypatch.setattr(settings_module, "_config_redis_client", None)
    monkeypatch.setattr(settings_module, "_config_redis_attempted", False)


# ---------------------------------------------------------------------------
# Default value tests
# ---------------------------------------------------------------------------


def test_defaults_when_env_unset(monkeypatch):
    settings = settings_module.Settings()

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


# ---------------------------------------------------------------------------
# Override tests
# ---------------------------------------------------------------------------


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "85")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", "/tmp/custom.db")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_RF", "2")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_XGB", "3")
    monkeypatch.setenv("ENSEMBLE_WEIGHT_LGBM", "5")

    settings = settings_module.Settings()

    assert settings.risk_score_threshold == 85
    assert settings.db_path == "/tmp/custom.db"
    assert settings.ensemble_weight_rf == 2
    assert settings.ensemble_weight_xgb == 3
    assert settings.ensemble_weight_lgbm == 5


# ---------------------------------------------------------------------------
# Ensemble weight validators
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CORS validators
# ---------------------------------------------------------------------------


def test_cors_wildcard_origin_raises(monkeypatch):
    monkeypatch.setenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValueError, match="must not contain '\\*'"):
        settings_module.Settings()


def test_cors_wildcard_in_list_raises(monkeypatch):
    monkeypatch.setenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", "https://ok.example.com,*")

    with pytest.raises(ValueError, match="must not contain '\\*'"):
        settings_module.Settings()


def test_cors_default_is_empty_tuple(monkeypatch):
    settings = settings_module.Settings()

    assert settings.cors_allowed_origins == ()


def test_cors_valid_origins_parsed_as_tuple(monkeypatch):
    monkeypatch.setenv(
        "LEDGERLENS_CORS_ALLOWED_ORIGINS",
        "https://dashboard.example.com,https://staging.example.com",
    )

    settings = settings_module.Settings()

    assert settings.cors_allowed_origins == (
        "https://dashboard.example.com",
        "https://staging.example.com",
    )


# ---------------------------------------------------------------------------
# Risk-score threshold validator
# ---------------------------------------------------------------------------


def test_risk_score_threshold_below_zero_raises(monkeypatch):
    """RISK_SCORE_THRESHOLD must be in [0, 100]."""
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "-1")

    with pytest.raises(ValueError, match="RISK_SCORE_THRESHOLD"):
        settings_module.Settings()


def test_risk_score_threshold_above_100_raises(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "101")

    with pytest.raises(ValueError, match="RISK_SCORE_THRESHOLD"):
        settings_module.Settings()


def test_risk_score_threshold_boundary_values_accepted(monkeypatch):
    """0 and 100 are both valid boundary values."""
    for boundary in ("0", "100"):
        monkeypatch.setenv("RISK_SCORE_THRESHOLD", boundary)
        settings = settings_module.Settings()
        assert settings.risk_score_threshold == int(boundary)


# ---------------------------------------------------------------------------
# Network validator
# ---------------------------------------------------------------------------


def test_network_defaults_to_testnet(monkeypatch):
    settings = settings_module.Settings()

    assert settings.network == "testnet"


def test_network_mainnet_accepted(monkeypatch):
    monkeypatch.setenv("NETWORK", "mainnet")

    settings = settings_module.Settings()

    assert settings.network == "mainnet"


def test_network_invalid_value_raises(monkeypatch):
    monkeypatch.setenv("NETWORK", "devnet")

    with pytest.raises(ValueError, match="NETWORK must be 'testnet' or 'mainnet'"):
        settings_module.Settings()


# ---------------------------------------------------------------------------
# Streamer validators
# ---------------------------------------------------------------------------


def test_streamer_overflow_strategy_invalid_raises(monkeypatch):
    monkeypatch.setenv("STREAMER_OVERFLOW_STRATEGY", "discard")

    with pytest.raises(ValueError, match="STREAMER_OVERFLOW_STRATEGY"):
        settings_module.Settings()


def test_streamer_overflow_strategy_valid_values(monkeypatch):
    for strategy in ("block", "drop_newest", "drop_oldest"):
        monkeypatch.setenv("STREAMER_OVERFLOW_STRATEGY", strategy)
        settings = settings_module.Settings()
        assert settings.streamer_overflow_strategy == strategy


def test_streamer_high_water_ratio_zero_raises(monkeypatch):
    monkeypatch.setenv("STREAMER_HIGH_WATER_RATIO", "0")

    with pytest.raises(ValueError, match="STREAMER_HIGH_WATER_RATIO"):
        settings_module.Settings()


def test_streamer_high_water_ratio_above_one_raises(monkeypatch):
    monkeypatch.setenv("STREAMER_HIGH_WATER_RATIO", "1.1")

    with pytest.raises(ValueError, match="STREAMER_HIGH_WATER_RATIO"):
        settings_module.Settings()


# ---------------------------------------------------------------------------
# Regression Tests
# ---------------------------------------------------------------------------

def test_no_duplicate_field_declarations():
    """Regression test for Issue #683: ensure fields are declared exactly once."""
    import ast
    from pathlib import Path
    import config.settings

    settings_path = Path(config.settings.__file__)
    tree = ast.parse(settings_path.read_text(encoding="utf-8"))

    # find the Settings class
    settings_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")

    # count assignments
    field_counts = {}
    for node in settings_class.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            field_name = node.target.id
            field_counts[field_name] = field_counts.get(field_name, 0) + 1

    for field in [
        "cost_per_vcpu_hour_usd",
        "cost_per_gb_memory_hour_usd",
        "cost_per_gb_storage_month_usd",
        "capacity_projection_window_days",
        "capacity_projection_lead_time_days",
    ]:
        assert field_counts.get(field, 0) == 1, f"Field {field} is declared {field_counts.get(field, 0)} times."
