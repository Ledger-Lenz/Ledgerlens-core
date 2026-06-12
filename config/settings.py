"""Central configuration loaded from environment variables (.env)."""

import os
from dataclasses import dataclass, field

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
    risk_score_threshold: int = field(default_factory=lambda: int(os.getenv("RISK_SCORE_THRESHOLD", "70")))

    model_dir: str = field(default_factory=lambda: os.getenv("MODEL_DIR", "./models"))

    ledgerlens_api_url: str = field(default_factory=lambda: os.getenv("LEDGERLENS_API_URL", "http://localhost:8000"))
    score_contract_id: str = field(default_factory=lambda: os.getenv("LEDGERLENS_SCORE_CONTRACT_ID", ""))
    service_secret_key: str = field(default_factory=lambda: os.getenv("LEDGERLENS_SERVICE_SECRET_KEY", ""))


settings = Settings()
