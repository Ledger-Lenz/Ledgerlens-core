"""Minimal configuration for the standalone federated learning server."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    db_path: str = "./ledgerlens_federated.db"
    
    # ── API / security ────────────────────────────────────────────────────────
    admin_api_key: str = ""

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
    federated_admission_required: bool = True
    federated_max_participant_weight_fraction: float = 0.5
    federated_max_n_samples_growth_factor: float = 3.0
    federated_use_krum: bool = True


settings = Settings()
