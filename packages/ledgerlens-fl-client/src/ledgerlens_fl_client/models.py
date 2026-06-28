from dataclasses import dataclass


@dataclass
class RoundResult:
    """Result of a federated learning round.
    
    Attributes
    ----------
    round_id : str
        Unique identifier for the federated round.
    accepted : bool
        Whether the update was accepted by the aggregation server.
    reason : str
        Server response reason (e.g., "ok", or exclusion reason).
    local_auc : float | None
        Local model AUC-ROC on held-out data (if computed).
    n_samples : int
        Number of samples used in this round.
    n_valid_pending : int
        Number of valid pending updates at aggregation server.
    quorum : int
        Minimum participants required for aggregation.
    """
    round_id: str
    accepted: bool
    reason: str
    local_auc: float | None
    n_samples: int
    n_valid_pending: int
    quorum: int


@dataclass
class ClientStatus:
    """Current status of the FL client.
    
    Attributes
    ----------
    operator_id : str
        Unique operator identifier.
    rounds_completed : int
        Number of federated rounds completed.
    has_models : bool
        Whether local models have been trained.
    public_key_der_b64 : str
        Base64-encoded DER public key for authentication.
    """
    operator_id: str
    rounds_completed: int
    has_models: bool
    public_key_der_b64: str