"""Central configuration loaded from environment variables (.env)."""

import os
from dataclasses import dataclass, field

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
    risk_score_threshold: int = field(default_factory=lambda: int(os.getenv("RISK_SCORE_THRESHOLD", "70")))
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
