"""Central configuration loaded from environment variables (.env)."""

import os
from dataclasses import dataclass, field
import time

from dotenv import load_dotenv

load_dotenv()


def _parse_evm_pool_addresses(raw: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in raw.split(",") if a.strip())


@dataclass(frozen=True)
class Settings:
    horizon_url: str = field(default_factory=lambda: os.getenv("HORIZON_URL", "https://horizon.stellar.org"))
    horizon_stream_url: str = field(default_factory=lambda: os.getenv("HORIZON_STREAM_URL", "https://horizon.stellar.org"))
    network: str = field(default_factory=lambda: os.getenv("NETWORK", "testnet"))

    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "5")))
    trade_history_lookback_days: int = field(default_factory=lambda: int(os.getenv("TRADE_HISTORY_LOOKBACK_DAYS", "30")))

    benford_mad_threshold: float = field(default_factory=lambda: float(os.getenv("BENFORD_MAD_THRESHOLD", "0.015")))
    benford_min_sample_count: int = field(default_factory=lambda: int(os.getenv("BENFORD_MIN_SAMPLE_COUNT", "30")))
    benford_max_window_days: int = field(default_factory=lambda: int(os.getenv("BENFORD_MAX_WINDOW_DAYS", "90")))
    # Causal feature selection (PC algorithm)
    causal_independence_alpha: float = field(
        default_factory=lambda: float(os.getenv("CAUSAL_INDEPENDENCE_ALPHA", "0.01"))
    )
    causal_max_conditioning_size: int = field(
        default_factory=lambda: int(os.getenv("CAUSAL_MAX_CONDITIONING_SIZE", "3"))
    )
    _default_risk_score_threshold: int = field(default_factory=lambda: int(os.getenv("RISK_SCORE_THRESHOLD", "70")))
    COMMITTEE_QUORUM: int = field(default_factory=lambda: int(os.getenv("COMMITTEE_QUORUM", "3")))
    COMMITTEE_VOTE_DEADLINE_DAYS: int = field(default_factory=lambda: int(os.getenv("COMMITTEE_VOTE_DEADLINE_DAYS", "14")))
    ensemble_weight_rf: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_RF", "0.25")))
    ensemble_weight_xgb: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_XGB", "0.50")))
    ensemble_weight_lgbm: float = field(default_factory=lambda: float(os.getenv("ENSEMBLE_WEIGHT_LGBM", "0.25")))
    temporal_weight: float = field(default_factory=lambda: float(os.getenv("TEMPORAL_WEIGHT", "0.3")))
    _runtime_cache_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("RUNTIME_CONFIG_TTL_SECONDS", "60")))

    # Fraction of the composite risk score driven by the sandwich-attack signal
    # (see detection.risk_score.RiskScore.combine). 0.0 preserves the legacy
    # benford/ML-only blend.
    sandwich_score_weight: float = field(
        default_factory=lambda: float(os.getenv("SANDWICH_SCORE_WEIGHT", "0.0"))
    )

    # Fraction of the composite risk score driven by the multivariate (cross-pair)
    # Benford copula dependence signal (see detection.risk_score.RiskScore.combine).
    # 0.0 preserves the legacy Benford/ML-only blend.
    benford_copula_weight: float = field(
        default_factory=lambda: float(os.getenv("BENFORD_COPULA_WEIGHT", "0.0"))
    )

    # Points subtracted per unit of positive price-discovery-contribution (PDC)
    # (see detection.risk_score.RiskScore.combine). 0.0 preserves the legacy
    # Benford/ML-only blend; negative PDC never increases the score.
    pdc_discount_weight: float = field(
        default_factory=lambda: float(os.getenv("PDC_DISCOUNT_WEIGHT", "0.0"))
    )

    model_dir: str = field(default_factory=lambda: os.getenv("MODEL_DIR", "./models"))
    db_path: str = field(default_factory=lambda: os.getenv("LEDGERLENS_DB_PATH", "./ledgerlens.db"))

    # Feature Store (Redis hot layer + SQLite cold layer)
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    feature_store_ttl_hours: int = field(default_factory=lambda: int(os.getenv("FEATURE_STORE_TTL_HOURS", "48")))
    feature_store_flush_interval_seconds: int = (
        field(default_factory=lambda: int(os.getenv("FEATURE_STORE_FLUSH_INTERVAL_SECONDS", "300")))
    )

    # Streaming
    cursor_path: str = field(default_factory=lambda: os.getenv("CURSOR_PATH", "./horizon_cursor.txt"))

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
    # Separate key scope gating the regulatory `/compliance/` endpoints so that
    # SAR / Travel-Rule exports are never reachable with the admin key alone.
    compliance_api_key: str = field(default_factory=lambda: os.getenv("LEDGERLENS_COMPLIANCE_API_KEY", ""))
    model_signing_key: str = field(default_factory=lambda: os.getenv("LEDGERLENS_MODEL_SIGNING_KEY", ""))

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
    # RDP noise multiplier (σ = clip_norm × noise_multiplier).
    # 0.0 disables RDP accounting and falls back to legacy linear ε tracking.
    # Production default: set FEDERATED_NOISE_MULTIPLIER=1.1 in .env.
    federated_noise_multiplier: float = field(
        default_factory=lambda: float(os.getenv("FEDERATED_NOISE_MULTIPLIER", "0.0"))
    )
    federated_server_host: str = field(
        default_factory=lambda: os.getenv("FEDERATED_SERVER_HOST", "127.0.0.1")
    )
    federated_server_port: int = field(
        default_factory=lambda: int(os.getenv("FEDERATED_SERVER_PORT", "8001"))
    )

    # EVM cross-chain detection
    evm_rpc_ethereum: str = field(
        default_factory=lambda: os.getenv("EVM_RPC_ETHEREUM", "https://eth.llamarpc.com")
    )
    evm_rpc_base: str = field(
        default_factory=lambda: os.getenv("EVM_RPC_BASE", "https://mainnet.base.org")
    )
    evm_rpc_polygon: str = field(
        default_factory=lambda: os.getenv("EVM_RPC_POLYGON", "https://polygon-rpc.com")
    )
    evm_lookback_blocks: int = field(
        default_factory=lambda: int(os.getenv("EVM_LOOKBACK_BLOCKS", "5760"))
    )
    evm_pool_addresses: tuple[str, ...] = field(
        default_factory=lambda: _parse_evm_pool_addresses(
            os.getenv("EVM_POOL_ADDRESSES", "")
        )
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
        self._validate_evm_pool_addresses()

    def _validate_evm_pool_addresses(self) -> None:
        if not self.evm_pool_addresses:
            return
        from web3 import Web3

        for addr in self.evm_pool_addresses:
            if not isinstance(addr, str) or len(addr) != 42 or not addr.startswith("0x"):
                raise ValueError(
                    f"EVM_POOL_ADDRESSES contains a malformed address: {addr!r}. "
                    "Addresses must be 42-character hex strings starting with '0x'."
                )
            if not Web3.is_checksum_address(addr):
                raise ValueError(
                    f"EVM_POOL_ADDRESSES contains a non-checksummed address: {addr!r}. "
                    "Use EIP-55 checksum format (e.g. '0xAb5801a7D398...')."
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

@runtime_risk_score_threshold.setter
def runtime_risk_score_threshold(self, value: int) -> None:
    object.__setattr__(self, "_default_risk_score_threshold", value)

# Monkeypatch onto Settings instance for attribute access
setattr(Settings, "risk_score_threshold", runtime_risk_score_threshold)
