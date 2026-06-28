"""Federated Learning client for LedgerLens exchange partners.

Provides a clean API for exchange operators to participate in federated
learning rounds without sharing raw trade data. Only gradient updates
(soft labels on a public dataset) are transmitted.

Example
-------
    from ledgerlens_fl_client import FLClient, DataAdapter
    import pandas as pd

    class MyExchangeAdapter(DataAdapter):
        def trade_batches(self):
            # Load your private trade data
            df = pd.read_csv("my_trades.csv")
            yield df

    client = FLClient(
        server_url="https://fl.ledgerlens.io",
        api_key="your-api-key",
        data_adapter=MyExchangeAdapter(),
        operator_id="exchange-xyz",
    )

    result = client.train_round()
    print(f"Round {result.round_id}: accepted={result.accepted}")
"""

from __future__ import annotations

import base64
import logging
import uuid

import numpy as np
import pandas as pd

from .adapter import DataAdapter
from ._core import (
    ensemble_predict_proba,
    clip_delta,
    inject_dp_noise,
    train_local_ensemble,
    update_with_distilled_labels,
    evaluate_ensemble,
)
from ._crypto import generate_keypair, public_key_to_der_b64, sign_payload
from ._protocol import FLProtocol
from ._public_dataset import get_public_dataset
from .models import RoundResult, ClientStatus

logger = logging.getLogger(__name__)


class FLClient:
    """Federated learning client for exchange-side participation.
    
    This client enables exchange operators to contribute to the LedgerLens
    federated learning network without exposing raw trade data. Only soft
    labels on a shared public dataset are transmitted.
    
    Parameters
    ----------
    server_url : str
        URL of the federated aggregation server.
    api_key : str
        API key for server authentication.
    data_adapter : DataAdapter
        Adapter providing local trade data batches.
    operator_id : str | None
        Unique operator identifier. Auto-generated if None.
    dp_epsilon : float
        Differential privacy epsilon (default: 1.0).
    dp_delta : float
        Differential privacy delta (default: 1e-5).
    gradient_clip_threshold : float
        L2 norm clip threshold (default: 10.0).
    noise_multiplier : float
        Noise multiplier for RDP path (default: 0.0).
    ensemble_weight_rf : float
        Random forest weight (default: 0.25).
    ensemble_weight_xgb : float
        XGBoost weight (default: 0.50).
    ensemble_weight_lgbm : float
        LightGBM weight (default: 0.25).
    http_timeout : float
        HTTP request timeout in seconds (default: 60.0).
    
    Example
    -------
        client = FLClient(
            server_url="https://fl.ledgerlens.io",
            api_key="...",
            data_adapter=my_adapter,
            operator_id="exchange-xyz",
        )
        result = client.train_round()
    """
    
    def __init__(
        self,
        server_url: str,
        api_key: str,
        data_adapter: DataAdapter,
        operator_id: str | None = None,
        dp_epsilon: float = 1.0,
        dp_delta: float = 1e-5,
        gradient_clip_threshold: float = 10.0,
        noise_multiplier: float = 0.0,
        ensemble_weight_rf: float = 0.25,
        ensemble_weight_xgb: float = 0.50,
        ensemble_weight_lgbm: float = 0.25,
        http_timeout: float = 60.0,
    ):
        self.operator_id = operator_id or str(uuid.uuid4())
        self.data_adapter = data_adapter
        self.server_url = server_url
        self.api_key = api_key
        
        self.dp_epsilon = dp_epsilon
        self.dp_delta = dp_delta
        self.gradient_clip_threshold = gradient_clip_threshold
        self.noise_multiplier = noise_multiplier
        
        self.ensemble_weights = {
            "random_forest": ensemble_weight_rf,
            "xgboost": ensemble_weight_xgb,
            "lightgbm": ensemble_weight_lgbm,
        }
        
        self._protocol: FLProtocol | None = None
        self._private_key, self._public_key_der = generate_keypair()
        self._models: dict | None = None
        self._prev_xgb_booster = None
        self._prev_lgbm_model = None
        self._rounds_completed = 0
        self._registered = False
        
        logger.info(
            "Initialized FLClient for operator %s (server: %s)",
            self.operator_id,
            server_url,
        )
    
    @property
    def public_key_der_b64(self) -> str:
        """Return base64-encoded DER public key."""
        return public_key_to_der_b64(self._private_key)
    
    def _ensure_protocol(self) -> FLProtocol:
        """Ensure HTTP protocol client is initialized."""
        if self._protocol is None:
            self._protocol = FLProtocol(
                server_url=self.server_url,
                api_key=self.api_key,
                timeout=self.http_timeout if hasattr(self, 'http_timeout') else 60.0,
            )
        return self._protocol
    
    def close(self) -> None:
        """Close the HTTP client."""
        if self._protocol is not None:
            self._protocol.close()
    
    def __enter__(self) -> FLClient:
        return self
    
    def __exit__(self, *exc_info) -> None:
        self.close()
    
    def _load_private_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Load and concatenate all private data from adapter.
        
        Returns
        -------
        tuple
            (X, y) feature matrix and labels.
        """
        all_dfs: list = []
        for batch in self.data_adapter.trade_batches():
            all_dfs.append(batch)
        
        if not all_dfs:
            raise ValueError("DataAdapter returned no data batches")
        
        df = pd.concat(all_dfs, ignore_index=True)
        
        if "label" not in df.columns:
            raise ValueError("DataFrame must contain 'label' column")
        
        feature_cols = [c for c in df.columns if c != "label"]
        X = df[feature_cols].fillna(0.0).values.astype(np.float64)
        y = df["label"].values.astype(int)
        
        return X, y
    
    def train_round(self, random_state: int = 42) -> RoundResult:
        """Execute one federated learning round.
        
        Orchestrates the full FL protocol:
        1. Register with server (if not already registered)
        2. Fetch current global model
        3. Load private data and train local ensemble
        4. Compute soft labels on public dataset
        5. Compute delta, clip, inject DP noise
        6. Sign and submit update to server
        7. Fetch updated global model and fine-tune with distilled labels
        8. Return RoundResult with metrics
        
        Parameters
        ----------
        random_state : int
            Random seed for reproducibility.
        
        Returns
        -------
        RoundResult
            Result containing round_id, acceptance status, and metrics.
        
        Raises
        ------
        httpx.HTTPStatusError
            If server communication fails.
        ValueError
            If data adapter returns no data.
        """
        
        protocol = self._ensure_protocol()
        
        if not self._registered:
            protocol.register(self.operator_id, self.public_key_der_b64)
            self._registered = True
            logger.info("Registered with server as %s", self.operator_id)
        
        global_model = protocol.fetch_global_model()
        round_id = global_model.round_id
        prev_global = global_model.global_soft_labels
        
        X_pub = get_public_dataset()
        if prev_global is None:
            prev_global = np.full(len(X_pub), 0.5)
        
        X_priv, y_priv = self._load_private_data()
        logger.info(
            "Loaded private data: %d samples, %d features",
            len(y_priv),
            X_priv.shape[1],
        )
        
        self._models, self._prev_xgb_booster, self._prev_lgbm_model = train_local_ensemble(
            X_priv, y_priv, random_state=random_state,
            prev_xgb_booster=self._prev_xgb_booster,
            prev_lgbm_model=self._prev_lgbm_model,
        )
        
        soft_labels = ensemble_predict_proba(self._models, X_pub, self.ensemble_weights)
        
        delta = soft_labels - prev_global
        delta = clip_delta(delta, self.gradient_clip_threshold)
        noisy_delta = inject_dp_noise(
            delta,
            self.gradient_clip_threshold,
            self.noise_multiplier,
            self.dp_epsilon,
            self.dp_delta,
        )
        noisy_soft_labels = np.clip(prev_global + noisy_delta, 0.0, 1.0)
        
        signature = sign_payload(
            self._private_key,
            self.operator_id,
            round_id,
            noisy_soft_labels,
            len(y_priv),
        )
        signature_b64 = base64.b64encode(signature).decode()
        
        result = protocol.submit_update(
            self.operator_id,
            noisy_soft_labels,
            len(y_priv),
            signature_b64,
        )
        
        accepted = result.get("accepted", False)
        reason = result.get("reason", "unknown")
        n_valid = result.get("pending_valid", 0)
        quorum = result.get("quorum", 0)
        
        logger.info(
            "Round %s submitted: accepted=%s, reason=%s, pending=%d/%d",
            round_id,
            accepted,
            reason,
            n_valid,
            quorum,
        )
        
        updated_model = protocol.fetch_global_model()
        if updated_model.global_soft_labels is not None:
            self._models, self._prev_xgb_booster, self._prev_lgbm_model = update_with_distilled_labels(
                X_priv, y_priv, X_pub, updated_model.global_soft_labels,
                self.ensemble_weights, random_state=random_state,
                prev_xgb_booster=self._prev_xgb_booster,
                prev_lgbm_model=self._prev_lgbm_model,
            )
            logger.info("Applied distillation update from global model")
        
        local_auc = None
        if len(np.unique(y_priv)) > 1:
            local_auc = evaluate_ensemble(self._models, X_priv, y_priv, self.ensemble_weights)
        
        self._rounds_completed += 1
        
        return RoundResult(
            round_id=round_id,
            accepted=accepted,
            reason=reason,
            local_auc=local_auc,
            n_samples=len(y_priv),
            n_valid_pending=n_valid,
            quorum=quorum,
        )
    
    def status(self) -> ClientStatus:
        """Return current client status.
        
        Returns
        -------
        ClientStatus
            Status including operator_id, rounds completed, and key info.
        """
        return ClientStatus(
            operator_id=self.operator_id,
            rounds_completed=self._rounds_completed,
            has_models=self._models is not None,
            public_key_der_b64=self.public_key_der_b64,
        )