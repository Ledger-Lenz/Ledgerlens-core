"""Central configuration loaded from environment variables (.env).

At import time, pydantic-settings validates every field. A missing required
field or type/range violation aborts startup immediately with a human-readable
error listing every problem at once.
"""

import time
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())

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
    # Sequence model settings
    temporal_model_type: str = field(default_factory=lambda: os.getenv("TEMPORAL_MODEL_TYPE", "lstm"))
    temporal_max_seq_len: int = field(default_factory=lambda: int(os.getenv("TEMPORAL_MAX_SEQ_LEN", "200")))
    temporal_lstm_hidden_dim: int = field(default_factory=lambda: int(os.getenv("TEMPORAL_LSTM_HIDDEN_DIM", "64")))
    _runtime_cache_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("RUNTIME_CONFIG_TTL_SECONDS", "60")))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── Horizon ───────────────────────────────────────────────────────────────
    horizon_url: str = "https://horizon.stellar.org"
    horizon_stream_url: str = "https://horizon.stellar.org"
    network: str = "testnet"
    horizon_default_cursor: str = "now"  # Used when no valid checkpoint exists.
    data_dir: str = "./data"  # Root allowed to contain runtime data files.
    cursor_checkpoint_path: str = "./data/horizon_cursor.json"  # Durable SSE offset.
    cursor_flush_events: int = 100  # Persist after this many processed events.
    cursor_flush_seconds: float = 10.0  # Persist after this many seconds.
    horizon_rate_limit: float = 50.0
    horizon_rate_bucket_capacity: float = 100.0
    streamer_queue_maxsize: int = 1000  # Hard cap on buffered Horizon trades.
    streamer_overflow_strategy: str = "drop_oldest"  # block/drop_newest/drop_oldest.
    streamer_high_water_ratio: float = 0.8  # Begin producer throttling at 80%.
    rate_restore_seconds: float = 60.0
    stream_checkpoint_interval: int = 100
    stream_score_delta_threshold: int = 5

    # ── Polling ───────────────────────────────────────────────────────────────
    poll_interval_seconds: int = 5
    trade_history_lookback_days: int = 30
    cursor_path: str = "./horizon_cursor.txt"
    # Keep this below Horizon's per-IP request limit divided by average request duration.
    historical_loader_concurrency: int = 4
    historical_chunk_hours: float = 6.0
    historical_progress_path: str = "./data/historical_progress.json"
    historical_max_lookback_days: int = 365

    # ── Feature Store ─────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    feature_store_ttl_hours: int = 48
    feature_store_flush_interval_seconds: int = 300

    # ── Detection ─────────────────────────────────────────────────────────────
    benford_mad_threshold: float = 0.015
    risk_score_threshold: int = 70
    committee_quorum: int = 3
    committee_vote_deadline_days: int = 14

    # ── Ensemble weights ──────────────────────────────────────────────────────
    ensemble_weight_rf: float = 0.25
    ensemble_weight_xgb: float = 0.50
    ensemble_weight_lgbm: float = 0.25

    # ── Score blending ────────────────────────────────────────────────────────
    temporal_weight: float = 0.3
    sandwich_score_weight: float = 0.0
    benford_copula_weight: float = 0.0
    pdc_discount_weight: float = 0.0

    # ── Storage ───────────────────────────────────────────────────────────────
    model_dir: str = "./models"
    ledgerlens_db_path: str = "./ledgerlens.db"

    # ── Downstream services ───────────────────────────────────────────────────
    ledgerlens_api_url: str = "http://localhost:8000"
    ledgerlens_score_contract_id: str = ""
    ledgerlens_service_secret_key: str = ""

    # ── Soroban ───────────────────────────────────────────────────────────────
    soroban_rpc_url: str = "https://soroban-testnet.stellar.org"
    network_passphrase: str = "Test SDF Network ; September 2015"
    soroban_circuit_breaker_threshold: int = 5
    soroban_circuit_reset_seconds: int = 300

    # ── API / security ────────────────────────────────────────────────────────
    ledgerlens_cors_allowed_origins: str = ""
    ledgerlens_admin_api_key: str = ""
    ledgerlens_compliance_api_key: str = ""
    ledgerlens_model_signing_key: str = ""
    ledgerlens_webhook_encryption_key: str = ""

    # ── ED25519 model signing ────────────────────────────────────────────────
    # Base64-encoded 32-byte ED25519 public key for model artifact signing.
    # Generate with: python cli.py generate-signing-key
    model_signing_public_key: str = ""

    # ── Federated learning ────────────────────────────────────────────────────
    federated_min_participants: int = 3
    federated_dp_epsilon: float = 1.0
    federated_dp_delta: float = 1e-5
    federated_dp_max_epsilon: float = 10.0
    gradient_clip_threshold: float = 10.0
    gradient_outlier_threshold: float = 0.1
    federated_noise_multiplier: float = 0.0
    federated_server_host: str = "127.0.0.1"
    federated_server_port: int = 8001

    # ── Cross-chain Bayesian linking ─────────────────────────────────────────
    cross_chain_timing_sigma_seconds: float = 300.0
    cross_chain_amount_tolerance: float = 0.005
    cross_chain_min_confidence: float = 0.70
    cross_chain_confirmed_confidence: float = 0.90

    # ── EVM cross-chain ───────────────────────────────────────────────────────
    evm_rpc_ethereum: str = "https://eth.llamarpc.com"
    evm_rpc_base: str = "https://mainnet.base.org"
    evm_rpc_polygon: str = "https://polygon-rpc.com"
    evm_lookback_blocks: int = 5760
    # Store as raw string; parsed tuple exposed via .evm_pool_addresses property
    evm_pool_addresses: str = ""

    # ── Runtime config cache TTL ──────────────────────────────────────────────
    runtime_config_ttl_seconds: int = 60

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("poll_interval_seconds", "trade_history_lookback_days",
                     "feature_store_ttl_hours", "feature_store_flush_interval_seconds",
                     "soroban_circuit_reset_seconds", "evm_lookback_blocks",
                     "committee_quorum", "committee_vote_deadline_days",
                     "federated_min_participants", "cursor_flush_events",
                     "stream_checkpoint_interval", "streamer_queue_maxsize",
                     "historical_loader_concurrency", "historical_max_lookback_days",
                     mode="before")
    @classmethod
    def must_be_positive(cls, v: object) -> object:
        if int(v) <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("federated_server_port", mode="before")
    @classmethod
    def valid_port(cls, v: object) -> object:
        port = int(v)
        if not (1 <= port <= 65535):
            raise ValueError(f"port {port} is out of range 1-65535")
        return v

    @field_validator("risk_score_threshold", mode="before")
    @classmethod
    def valid_score_threshold(cls, v: object) -> object:
        val = int(v)
        if not (0 <= val <= 100):
            raise ValueError(f"RISK_SCORE_THRESHOLD {val} must be 0-100")
        return v

    @field_validator("soroban_circuit_breaker_threshold", mode="before")
    @classmethod
    def valid_circuit_threshold(cls, v: object) -> object:
        if int(v) < 1:
            raise ValueError("SOROBAN_CIRCUIT_BREAKER_THRESHOLD must be >= 1")
        return v

    @field_validator("compliance_sar_min_score", mode="before")
    @classmethod
    def valid_sar_min_score(cls, v: object) -> object:
        val = int(v)
        if not (0 <= val <= 100):
            raise ValueError(f"COMPLIANCE_SAR_MIN_SCORE {val} must be 0-100")
        return v

    @field_validator("compliance_export_rate_limit_per_hour", mode="before")
    @classmethod
    def valid_export_rate_limit(cls, v: object) -> object:
        if int(v) < 1:
            raise ValueError("COMPLIANCE_EXPORT_RATE_LIMIT_PER_HOUR must be >= 1")
        return v

    @field_validator("benford_mad_threshold", "temporal_weight",
                     "sandwich_score_weight", "benford_copula_weight",
                     "pdc_discount_weight", mode="before")
    @classmethod
    def non_negative_float(cls, v: object) -> object:
        if float(v) < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("cursor_flush_seconds", "historical_chunk_hours", mode="before")
    @classmethod
    def positive_cursor_flush_seconds(cls, v: object) -> object:
        if float(v) <= 0:
            raise ValueError("CURSOR_FLUSH_SECONDS must be positive")
        return v

    @field_validator("streamer_overflow_strategy", mode="before")
    @classmethod
    def valid_streamer_overflow_strategy(cls, v: object) -> object:
        strategy = str(v).strip().lower()
        if strategy not in {"block", "drop_newest", "drop_oldest"}:
            raise ValueError(
                "STREAMER_OVERFLOW_STRATEGY must be block, drop_newest, or drop_oldest"
            )
        return strategy

    @field_validator("streamer_high_water_ratio", mode="before")
    @classmethod
    def valid_streamer_high_water_ratio(cls, v: object) -> object:
        ratio = float(v)
        if not 0 < ratio <= 1:
            raise ValueError("STREAMER_HIGH_WATER_RATIO must be in (0, 1]")
        return ratio

    @field_validator("horizon_default_cursor", mode="before")
    @classmethod
    def valid_horizon_default_cursor(cls, v: object) -> object:
        import re

        cursor = str(v).strip()
        if cursor != "now" and re.fullmatch(r"\d+-\d+", cursor) is None:
            raise ValueError("HORIZON_DEFAULT_CURSOR must be 'now' or a paging token")
        return cursor

    @field_validator("ensemble_weight_rf", "ensemble_weight_xgb", "ensemble_weight_lgbm",
                     mode="before")
    @classmethod
    def non_negative_weight(cls, v: object) -> object:
        if float(v) < 0:
            raise ValueError("Ensemble weights must be non-negative")
        return v

    @field_validator("horizon_url", "horizon_stream_url", "soroban_rpc_url",
                     "ledgerlens_api_url", "redis_url",
                     "evm_rpc_ethereum", "evm_rpc_base", "evm_rpc_polygon",
                     mode="before")
    @classmethod
    def valid_url(cls, v: object) -> object:
        s = str(v).strip()
        if not s:
            raise ValueError("must be a non-empty URL")
        if not (s.startswith("http://") or s.startswith("https://")
                or s.startswith("redis://") or s.startswith("rediss://")):
            raise ValueError(f"{s!r} is not a valid URL (expected http/https/redis scheme)")
        return s

    @field_validator("network", mode="before")
    @classmethod
    def valid_network(cls, v: object) -> object:
        val = str(v).strip().lower()
        if val not in ("testnet", "mainnet"):
            raise ValueError(f"NETWORK must be 'testnet' or 'mainnet', got {v!r}")
        return val

    @model_validator(mode="after")
    def ensemble_weights_not_all_zero(self) -> "Settings":
        if self.ensemble_weight_rf == 0 and self.ensemble_weight_xgb == 0 and self.ensemble_weight_lgbm == 0:
            raise ValueError("At least one ensemble weight must be positive")
        return self

    @model_validator(mode="after")
    def no_wildcard_cors(self) -> "Settings":
        if "*" in _split_csv(self.ledgerlens_cors_allowed_origins):
            raise ValueError(
                "LEDGERLENS_CORS_ALLOWED_ORIGINS must not contain '*'. "
                "Specify an explicit origin list instead."
            )
        return self

    @model_validator(mode="after")
    def checkpoint_path_is_inside_data_directory(self) -> "Settings":
        data_root = Path(self.data_dir).expanduser().resolve()
        for field_name, raw_path in (
            ("CURSOR_CHECKPOINT_PATH", self.cursor_checkpoint_path),
            ("HISTORICAL_PROGRESS_PATH", self.historical_progress_path),
        ):
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            else:
                candidate = candidate.resolve()
            try:
                candidate.relative_to(data_root)
            except ValueError as exc:
                raise ValueError(f"{field_name} must remain inside DATA_DIR") from exc
        return self

    @model_validator(mode="after")
    def valid_evm_pool_addresses(self) -> "Settings":
        addrs = _split_csv(self.evm_pool_addresses)
        if not addrs:
            return self
        from web3 import Web3
        for addr in addrs:
            if len(addr) != 42 or not addr.startswith("0x"):
                raise ValueError(f"EVM_POOL_ADDRESSES malformed address: {addr!r}")
            if not Web3.is_checksum_address(addr):
                raise ValueError(f"EVM_POOL_ADDRESSES non-checksummed address: {addr!r}")
        return self

    # ── Backward-compat properties ────────────────────────────────────────────

    @property
    def db_path(self) -> str:
        return self.ledgerlens_db_path

    @property
    def score_contract_id(self) -> str:
        return self.ledgerlens_score_contract_id

    @property
    def service_secret_key(self) -> str:
        return self.ledgerlens_service_secret_key

    @property
    def cors_allowed_origins(self) -> tuple[str, ...]:
        return _split_csv(self.ledgerlens_cors_allowed_origins)

    @property
    def admin_api_key(self) -> str:
        return self.ledgerlens_admin_api_key

    @property
    def compliance_api_key(self) -> str:
        return self.ledgerlens_compliance_api_key

    @property
    def model_signing_key(self) -> str:
        return self.ledgerlens_model_signing_key

    @property
    def _default_risk_score_threshold(self) -> int:
        return self.risk_score_threshold

    @property
    def _runtime_cache_ttl_seconds(self) -> int:
        return self.runtime_config_ttl_seconds


settings = Settings()


# ── Runtime config cache ──────────────────────────────────────────────────────
_runtime_cache: dict = {"ts": 0, "config": {}}


def load_runtime_config() -> dict:
    """Load runtime overrides from the `runtime_config` table with a TTL cache."""
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
    try:
        return int(cfg["risk_score_threshold"])
    except (KeyError, ValueError):
        return settings._default_risk_score_threshold
