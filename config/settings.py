"""Central configuration loaded from environment variables (.env)."""

import os
from dataclasses import dataclass, field
import time

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    horizon_url: str = field(default_factory=lambda: os.getenv("HORIZON_URL", "https://horizon.stellar.org"))
    horizon_stream_url: str = field(default_factory=lambda: os.getenv("HORIZON_STREAM_URL", "https://horizon.stellar.org"))
    network: str = field(default_factory=lambda: os.getenv("NETWORK", "testnet"))

    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "5")))
    trade_history_lookback_days: int = field(default_factory=lambda: int(os.getenv("TRADE_HISTORY_LOOKBACK_DAYS", "30")))

    benford_mad_threshold: float = field(default_factory=lambda: float(os.getenv("BENFORD_MAD_THRESHOLD", "0.015")))
    _default_risk_score_threshold: int = field(default_factory=lambda: int(os.getenv("RISK_SCORE_THRESHOLD", "70")))
    COMMITTEE_QUORUM: int = field(default_factory=lambda: int(os.getenv("COMMITTEE_QUORUM", "3")))
    COMMITTEE_VOTE_DEADLINE_DAYS: int = field(default_factory=lambda: int(os.getenv("COMMITTEE_VOTE_DEADLINE_DAYS", "14")))
    ensemble_weight_rf: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_RF", "0.25")))
    ensemble_weight_xgb: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_XGB", "0.50")))
    ensemble_weight_lgbm: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_LGBM", "0.25")))

    model_dir: str = field(default_factory=lambda: os.getenv("MODEL_DIR", "./models"))
    db_path: str = field(default_factory=lambda: os.getenv("LEDGERLENS_DB_PATH", "./ledgerlens.db"))

    ledgerlens_api_url: str = field(default_factory=lambda: os.getenv("LEDGERLENS_API_URL", "http://localhost:8000"))
    score_contract_id: str = field(default_factory=lambda: os.getenv("LEDGERLENS_SCORE_CONTRACT_ID", ""))
    service_secret_key: str = field(default_factory=lambda: os.getenv("LEDGERLENS_SERVICE_SECRET_KEY", ""))

    soroban_rpc_url: str = field(default_factory=lambda: os.getenv("SOROBAN_RPC_URL", "https://soroban-testnet.stellar.org"))
    network_passphrase: str = field(default_factory=lambda: os.getenv("NETWORK_PASSPHRASE", "Test SDF Network ; September 2015"))
    soroban_circuit_breaker_threshold: int = field(default_factory=lambda: int(os.getenv("SOROBAN_CIRCUIT_BREAKER_THRESHOLD", "5")))
    soroban_circuit_reset_seconds: int = field(default_factory=lambda: int(os.getenv("SOROBAN_CIRCUIT_RESET_SECONDS", "300")))

    cors_allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.getenv("LEDGERLENS_CORS_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        )
    )
    admin_api_key: str = field(default_factory=lambda: os.getenv("LEDGERLENS_ADMIN_API_KEY", ""))

    # Federated Learning
    federated_min_participants: int = field(
        default_factory=lambda: int(os.getenv("FEDERATED_MIN_PARTICIPANTS", "3"))
    )
    federated_dp_epsilon: float = field(
        default_factory=lambda: float(os.getenv("FEDERATED_DP_EPSILON", "1.0"))
    )
    federated_dp_delta: float = field(
        default_factory=lambda: float(os.getenv("FEDERATED_DP_DELTA", "1e-5"))
    )
    federated_dp_max_epsilon: float = field(
        default_factory=lambda: float(os.getenv("FEDERATED_DP_MAX_EPSILON", "10.0"))
    )
    gradient_clip_threshold: float = field(
        default_factory=lambda: float(os.getenv("GRADIENT_CLIP_THRESHOLD", "10.0"))
    )
    gradient_outlier_threshold: float = field(
        default_factory=lambda: float(os.getenv("GRADIENT_OUTLIER_THRESHOLD", "0.1"))
    )
    federated_server_host: str = field(
        default_factory=lambda: os.getenv("FEDERATED_SERVER_HOST", "127.0.0.1")
    )
    federated_server_port: int = field(
        default_factory=lambda: int(os.getenv("FEDERATED_SERVER_PORT", "8001"))
    )

    def __post_init__(self) -> None:
        weights = (
            self.ensemble_weight_rf,
            self.ensemble_weight_xgb,
            self.ensemble_weight_lgbm,
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("Ensemble weights must be non-negative")
        if all(weight == 0 for weight in weights):
            raise ValueError("At least one ensemble weight must be positive")
        if "*" in self.cors_allowed_origins:
            raise ValueError(
                "LEDGERLENS_CORS_ALLOWED_ORIGINS must not contain '*'. "
                "Specify an explicit origin list instead."
            )


settings = Settings()


# Runtime config cache
_runtime_cache: dict = {"ts": 0, "config": {}}


def load_runtime_config() -> dict:
    """Load runtime overrides from the `runtime_config` table with a TTL cache.

    Returns a dict of key->value strings. Cache TTL is configurable via
    `RUNTIME_CONFIG_TTL_SECONDS` environment variable (default 60).
    """
    now = time.time()
    ttl = settings._runtime_cache_ttl_seconds
    if _runtime_cache.get("ts", 0) + ttl > now and _runtime_cache.get("config"):
        return _runtime_cache["config"]

    import sqlite3

    config: dict = {}
    try:
        conn = sqlite3.connect(settings.db_path)
        cur = conn.execute("SELECT key, value FROM runtime_config")
        for k, v in cur.fetchall():
            config[k] = v
        conn.close()
    except Exception:
        config = {}

    _runtime_cache["ts"] = now
    _runtime_cache["config"] = config
    return config


def get_runtime_risk_score_threshold() -> int:
    cfg = load_runtime_config()
    if "risk_score_threshold" in cfg:
        try:
            return int(cfg["risk_score_threshold"])
        except Exception:
            return settings._default_risk_score_threshold
    return settings._default_risk_score_threshold


# Expose risk_score_threshold property for compatibility
@property
def runtime_risk_score_threshold(self) -> int:  # type: ignore
    return get_runtime_risk_score_threshold()

# Monkeypatch onto settings instance for attribute access
setattr(Settings, "risk_score_threshold", runtime_risk_score_threshold)
