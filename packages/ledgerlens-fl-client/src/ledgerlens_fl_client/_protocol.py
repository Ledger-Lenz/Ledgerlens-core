"""HTTP transport layer for FL client-server communication.

Implements the protocol expected by FederatedAggregationServer's FastAPI
endpoints: /federated/register, /federated/update, /federated/global-model,
/federated/server-public-key.
"""

from __future__ import annotations

import base64
import logging

import httpx
import numpy as np

logger = logging.getLogger(__name__)


class GlobalModelResponse:
    """Response from GET /federated/global-model.
    
    Attributes
    ----------
    round_id : str
        Current round UUID.
    global_soft_labels : np.ndarray | None
        Aggregated soft labels (None if not yet available).
    """
    
    def __init__(self, round_id: str, global_soft_labels: np.ndarray | None):
        self.round_id = round_id
        self.global_soft_labels = global_soft_labels


class FLProtocol:
    """HTTP client for FL server communication.
    
    Parameters
    ----------
    server_url : str
        Base URL of the federated aggregation server.
    api_key : str | None
        Optional API key for authentication (sent in X-API-Key header).
    timeout : float
        HTTP request timeout in seconds.
    """
    
    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        
        self._client = httpx.Client(
            base_url=self.server_url,
            timeout=timeout,
            headers=headers,
        )
    
    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
    
    def __enter__(self) -> FLProtocol:
        return self
    
    def __exit__(self, *exc_info) -> None:
        self.close()
    
    def register(self, participant_id: str, public_key_der_b64: str) -> dict:
        """Register participant with the FL server.
        
        Parameters
        ----------
        participant_id : str
            Unique operator identifier.
        public_key_der_b64 : str
            Base64-encoded DER public key.
        
        Returns
        -------
        dict
            Server response ({"status": "registered"}).
        
        Raises
        ------
        httpx.HTTPStatusError
            If registration fails (e.g., duplicate ID).
        """
        response = self._client.post(
            "/federated/register",
            json={
                "participant_id": participant_id,
                "public_key_der_b64": public_key_der_b64,
            },
        )
        response.raise_for_status()
        return response.json()
    
    def submit_update(
        self,
        participant_id: str,
        soft_labels: np.ndarray,
        n_samples: int,
        signature_b64: str,
    ) -> dict:
        """Submit a federated learning update to the server.
        
        Parameters
        ----------
        participant_id : str
            Operator identifier.
        soft_labels : np.ndarray
            Noisy soft label predictions.
        n_samples : int
            Number of training samples used.
        signature_b64 : str
            Base64-encoded Ed25519 signature.
        
        Returns
        -------
        dict
            Server response with keys: accepted, reason, pending_valid, quorum.
        
        Raises
        ------
        httpx.HTTPStatusError
            If submission fails (e.g., invalid signature, budget exhausted).
        """
        soft_labels_b64 = base64.b64encode(soft_labels.tobytes()).decode()
        
        response = self._client.post(
            "/federated/update",
            json={
                "participant_id": participant_id,
                "soft_labels_b64": soft_labels_b64,
                "n_samples": n_samples,
                "signature_b64": signature_b64,
            },
        )
        response.raise_for_status()
        return response.json()
    
    def fetch_global_model(self) -> GlobalModelResponse:
        """Fetch the current global model from the server.
        
        Returns
        -------
        GlobalModelResponse
            Current round ID and global soft labels (or None).
        """
        response = self._client.get("/federated/global-model")
        response.raise_for_status()
        data = response.json()
        
        global_soft_labels = None
        if data.get("global_soft_labels_b64"):
            global_soft_labels = np.frombuffer(
                base64.b64decode(data["global_soft_labels_b64"]),
                dtype=np.float64,
            )
        
        return GlobalModelResponse(
            round_id=data["round_id"],
            global_soft_labels=global_soft_labels,
        )
    
    def fetch_server_public_key(self) -> bytes:
        """Fetch the server's public key for audit verification.
        
        Returns
        -------
        bytes
            DER-encoded server public key.
        """
        response = self._client.get("/federated/server-public-key")
        response.raise_for_status()
        data = response.json()
        return base64.b64decode(data["public_key_der_b64"])