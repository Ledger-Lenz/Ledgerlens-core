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

    # ── Horizon API version guard ─────────────────────────────────────────────
    # The Stellar Horizon API evolves over time; breaking schema changes
    # (field renames, type changes, new required fields) can silently corrupt
    # ingested data if the client is written against a different version.
    #
    # VersionGuard reads the ``X-Stellar-Horizon-Version`` response header and
    # raises ``HorizonVersionError`` when the server version falls outside the
    # ``[HORIZON_MIN_VERSION, HORIZON_MAX_VERSION)`` range.
    #
    # How to update after a Horizon upgrade
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # 1. Review the Horizon changelog for the new version:
    #    https://github.com/stellar/go/blob/master/services/horizon/CHANGELOG.md
    # 2. Verify schema compatibility with the current ``data_models.py``.
    # 3. Update HORIZON_TESTED_VERSION to the new version string.
    # 4. Widen HORIZON_MIN_VERSION / HORIZON_MAX_VERSION if the new version
    #    introduces a new supported major version.
    # 5. If a breaking change requires a data-model update, do that in the same
    #    change set and bump HORIZON_TESTED_VERSION to the first version that
    #    carries the new schema.
    #
    # Setting HORIZON_VERSION_CHECK_ENABLED=false is an escape hatch for
    # private or custom Horizon nodes that strip the version header.  It MUST
    # NOT be used in production without understanding the schema compatibility
    # implications; a WARNING is logged at startup when disabled.
    horizon_min_version: str = "2.0.0"
    """Inclusive lower bound of the supported Horizon server version range."""

    horizon_max_version: str = "4.0.0"
    """Exclusive upper bound of the supported Horizon server version range."""

    horizon_tested_version: str = "2.28.0"
    """The specific Horizon version against which the current data models were validated.
    A WARNING is emitted when the live server reports a different (but in-range) version."""

    horizon_version_check_enabled: bool = True
    """Set to False to disable Horizon version header validation.
    Useful for private or custom Horizon nodes that omit the header.
    A WARNING is logged at startup when this is False."""
    horizon_default_cursor: str = "now"  # Used when no valid checkpoint exists.
    data_dir: str = "./data"  # Root allowed to contain runtime data files.
    cursor_checkpoint_path: str = "./data/horizon_cursor.json"  # Durable SSE offset.
    cursor_flush_events: int = 100  # Persist after this many processed events.
    cursor_flush_seconds: float = 10.0  # Persist after this many seconds.
    horizon_rate_limit: float = 50.0
    horizon_rate_bucket_capacity: float = 100.0

    # ── Horizon HTTP retry / rate-limit budget ────────────────────────────────
    # These five variables control RetryingHorizonClient's token-bucket rate
    # limiter and full-jitter retry loop.
    #
    # HORIZON_RATE_LIMIT_RPS is the steady-state request budget in requests per
    # second.  Set it to a value comfortably below Horizon's documented per-IP
    # limit (currently 3 500 req/hour ≈ ~0.97 req/s on the public testnet, or
    # higher on a private node).  The default of 5.0 is appropriate for the
    # public testnet; raise it when pointing at a dedicated mainnet node.
    #
    # HORIZON_RATE_BURST sets the bucket capacity.  A burst of 10 means up to
    # 10 requests can be dispatched immediately when the bucket is full before
    # throttling kicks in.  Keep burst ≥ HISTORICAL_LOADER_CONCURRENCY so
    # the parallel loader can dispatch its first batch without waiting.
    #
    # HORIZON_MAX_RETRY_DELAY is a hard upper bound on any single retry sleep,
    # including values from the server's Retry-After header.  This prevents a
    # malicious proxy from stalling the pipeline with Retry-After: 999999.
    horizon_rate_limit_rps: float = 5.0
    horizon_rate_burst: float = 10.0
    horizon_max_retries: int = 5
    horizon_base_retry_delay: float = 1.0
    horizon_max_retry_delay: float = 60.0
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

    # Multi-provider failover pool (ISSUE-013)
    # JSON-encoded list of provider objects.  Each object must have:
    #   chain_id (int), rpc_url (str, https:// only), name (str),
    #   priority (int, optional), max_requests_per_second (float, optional)
    # Example:
    #   EVM_PROVIDERS=[
    #     {"chain_id": 1, "rpc_url": "https://mainnet.infura.io/v3/KEY",
    #      "name": "infura", "priority": 0},
    #     {"chain_id": 1, "rpc_url": "https://eth-mainnet.alchemyapi.io/v2/KEY",
    #      "name": "alchemy", "priority": 1}
    #   ]
    # Leave empty ("[]") to rely solely on the legacy evm_rpc_* settings.
    evm_providers: str = "[]"

    # Maximum blocks behind the chain head before a provider's health is
    # considered degraded (also triggers lag alerts when ALL providers lag).
    evm_max_block_lag: int = 10

    # Seconds between health probe cycles (eth_blockNumber polls).
    evm_probe_interval_seconds: float = 15.0

    # Consecutive failures before a provider's circuit breaker opens.
    evm_circuit_breaker_threshold: int = 5

    # ── Runtime config cache TTL ──────────────────────────────────────────────
    runtime_config_ttl_seconds: int = 60

    # ── MLflow integration ────────────────────────────────────────────────────
    mlflow_tracking_uri: str = ""
    mlflow_experiment_name: str = "ledgerlens-training"
    mlflow_tracking_enabled: bool = False

    # ── Performance monitoring ────────────────────────────────────────────────
    performance_min_feedback_samples: int = 20
    performance_monitoring_window_days: int = 30

    # ── Feature Store Archival ────────────────────────────────────────────────
    feature_archive_dir: str = "./feature_archive"
    feature_archive_cutoff_days: int = 30

    # ── Bridge / cross-chain verification ────────────────────────────────────
    bridge_verify_receipt_timeout_seconds: int = 30
    bridge_verify_sample_rate: float = 1.0

    # ── Path payment loader ───────────────────────────────────────────────────
    path_payment_loader_enabled: bool = True
    path_payment_fetch_effects: bool = True

    # ── Prometheus metrics ────────────────────────────────────────────────────
    # Set to False to disable all prometheus_client metric collection.  When
    # disabled, ingestion/metrics.py returns a _NoOpCollector and GET /metrics
    # returns HTTP 503.  Useful in lightweight environments where prometheus_client
    # is not installed.
    metrics_enabled: bool = True
    # URL path at which the Prometheus text metrics are served by the local API.
    # Must start with "/" and contain no dynamic segments.
    metrics_endpoint: str = "/metrics"

    # ── Parquet export (ledgerlens-data integration) ──────────────────────────
    # Default root directory for `cli.py export-parquet` output.
    # Resolved relative to the working directory; must remain inside the project.
    parquet_export_dir: str = "./data/parquet_export"
    # Default Parquet compression codec: snappy | zstd | gzip | none
    parquet_compression: str = "snappy"
    # Rows per Parquet row group — larger = faster scans, higher write memory.
    parquet_row_group_size: int = 100_000

    # ── Trade filter pipeline ─────────────────────────────────────────────────
    # Path to the YAML file that configures the ingestion filter pipeline.
    # See config/filter_config.yaml.example for the full schema.
    filter_config_path: str = "./config/filter_config.yaml"
    # How often (seconds) FilterConfigLoader polls filter_config.yaml for changes.
    # Set to 0 to disable hot-reload (not recommended in production).
    filter_config_reload_interval_seconds: int = 60
    # When True, rejected trades are persisted to the `filtered_trades` SQLite
    # table so operators can review and tune filter rules without losing data.
    filter_store_rejected_trades: bool = True
    # Maximum rows in the `filtered_trades` table before pruning.
    # When exceeded, the oldest rows are deleted until the count reaches
    # 90 % of this limit (450 000 rows at the default).
    filter_rejected_trades_max_rows: int = 500_000

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

    @field_validator("compliance_sar_min_score", mode="before", check_fields=False)
    @classmethod
    def valid_sar_min_score(cls, v: object) -> object:
        val = int(v)
        if not (0 <= val <= 100):
            raise ValueError(f"COMPLIANCE_SAR_MIN_SCORE {val} must be 0-100")
        return v

    @field_validator("compliance_export_rate_limit_per_hour", mode="before", check_fields=False)
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

    @field_validator("feature_archive_cutoff_days", mode="before")
    @classmethod
    def valid_archive_cutoff(cls, v: object) -> object:
        if int(v) < 1:
            raise ValueError("FEATURE_ARCHIVE_CUTOFF_DAYS must be >= 1")
        return v

    @field_validator("metrics_endpoint", mode="before")
    @classmethod
    def valid_metrics_endpoint(cls, v: object) -> object:
        s = str(v).strip()
        if not s.startswith("/"):
            raise ValueError("METRICS_ENDPOINT must start with '/'")
        if "{" in s or "}" in s:
            raise ValueError("METRICS_ENDPOINT must not contain dynamic path segments")
        return s

    @field_validator("parquet_compression", mode="before")
    @classmethod
    def valid_parquet_compression(cls, v: object) -> object:
        val = str(v).strip().lower()
        allowed = {"snappy", "zstd", "gzip", "none"}
        if val not in allowed:
            raise ValueError(
                f"PARQUET_COMPRESSION must be one of {sorted(allowed)}, got {v!r}"
            )
        return val

    @field_validator("parquet_row_group_size", mode="before")
    @classmethod
    def valid_parquet_row_group_size(cls, v: object) -> object:
        val = int(v)
        if val < 1:
            raise ValueError("PARQUET_ROW_GROUP_SIZE must be >= 1")
        return val

    @field_validator("filter_config_reload_interval_seconds", mode="before")
    @classmethod
    def valid_filter_reload_interval(cls, v: object) -> object:
        val = int(v)
        if val < 0:
            raise ValueError("FILTER_CONFIG_RELOAD_INTERVAL_SECONDS must be >= 0")
        return val

    @field_validator("filter_rejected_trades_max_rows", mode="before")
    @classmethod
    def valid_filter_max_rows(cls, v: object) -> object:
        val = int(v)
        if val < 1:
            raise ValueError("FILTER_REJECTED_TRADES_MAX_ROWS must be >= 1")
        return val

    @model_validator(mode="after")
    def parquet_export_dir_no_traversal(self) -> "Settings":
        cwd = Path.cwd().resolve()
        candidate = Path(self.parquet_export_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(cwd)
        except ValueError as exc:
            raise ValueError(
                "PARQUET_EXPORT_DIR must be within the application working directory"
            ) from exc
        return self

    @model_validator(mode="after")
    def feature_archive_dir_no_traversal(self) -> "Settings":
        cwd = Path.cwd().resolve()
        candidate = Path(self.feature_archive_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(cwd)
        except ValueError as exc:
            raise ValueError(
                "FEATURE_ARCHIVE_DIR must be within the application working directory"
            ) from exc
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

    @field_validator("evm_max_block_lag", mode="before")
    @classmethod
    def valid_evm_max_block_lag(cls, v: object) -> object:
        val = int(v)
        if val < 1:
            raise ValueError("EVM_MAX_BLOCK_LAG must be >= 1")
        return val

    @field_validator("evm_probe_interval_seconds", mode="before")
    @classmethod
    def valid_evm_probe_interval(cls, v: object) -> object:
        val = float(v)
        if val <= 0:
            raise ValueError("EVM_PROBE_INTERVAL_SECONDS must be positive")
        return val

    @field_validator("evm_circuit_breaker_threshold", mode="before")
    @classmethod
    def valid_evm_circuit_breaker_threshold(cls, v: object) -> object:
        val = int(v)
        if val < 1:
            raise ValueError("EVM_CIRCUIT_BREAKER_THRESHOLD must be >= 1")
        return val

    @model_validator(mode="after")
    def valid_evm_providers_json(self) -> "Settings":
        """Validate EVM_PROVIDERS is a well-formed JSON list of provider objects.

        Each entry must have:
          - ``chain_id``: positive integer
          - ``rpc_url``: https:// URL (API keys in plaintext over http:// are rejected)
          - ``name``: non-empty string

        Optional fields: ``priority`` (int), ``max_requests_per_second`` (float > 0).
        """
        import json

        raw = self.evm_providers.strip()
        if not raw or raw == "[]":
            return self

        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"EVM_PROVIDERS is not valid JSON: {exc}") from exc

        if not isinstance(entries, list):
            raise ValueError("EVM_PROVIDERS must be a JSON array")

        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"EVM_PROVIDERS[{i}] must be a JSON object")

            # chain_id
            chain_id = entry.get("chain_id")
            if not isinstance(chain_id, int) or chain_id <= 0:
                raise ValueError(
                    f"EVM_PROVIDERS[{i}].chain_id must be a positive integer, "
                    f"got {chain_id!r}"
                )

            # rpc_url — must be https:// to protect embedded API keys
            rpc_url = entry.get("rpc_url", "")
            if not isinstance(rpc_url, str) or not rpc_url:
                raise ValueError(f"EVM_PROVIDERS[{i}].rpc_url must be a non-empty string")
            if not rpc_url.startswith("https://"):
                raise ValueError(
                    f"EVM_PROVIDERS[{i}].rpc_url must use https:// scheme. "
                    f"HTTP endpoints transmit API keys in plaintext."
                )

            # name
            name = entry.get("name", "")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"EVM_PROVIDERS[{i}].name must be a non-empty string"
                )

            # optional priority
            if "priority" in entry:
                if not isinstance(entry["priority"], int):
                    raise ValueError(f"EVM_PROVIDERS[{i}].priority must be an integer")

            # optional max_requests_per_second
            if "max_requests_per_second" in entry:
                mrps = entry["max_requests_per_second"]
                if not isinstance(mrps, (int, float)) or float(mrps) <= 0:
                    raise ValueError(
                        f"EVM_PROVIDERS[{i}].max_requests_per_second must be > 0"
                    )

        return self

    # ── Backward-compat properties ────────────────────────────────────────────

    @property
    def db_path(self) -> str:
        return self.ledgerlens_db_path

    @db_path.setter
    def db_path(self, value: str) -> None:
        object.__setattr__(self, "ledgerlens_db_path", value)

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

    @model_signing_key.setter
    def model_signing_key(self, value: str) -> None:
        object.__setattr__(self, "ledgerlens_model_signing_key", value)

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
