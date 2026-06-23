from stellar_sdk import Keypair
from typing import Optional

class OracleNode:
    def __init__(self, keypair: Keypair, contract_id: str, horizon_url: str):
        self.keypair = keypair
        self.contract_id = contract_id
        self.horizon_url = horizon_url

    def submit_score(self, wallet: str, score: float, round_id: int) -> str:
        """Signs and submits score to the Soroban oracle aggregation contract."""
        # TODO: Build transaction
        # TODO: Sign with keypair
        # TODO: Submit to Soroban
        pass

    def run_oracle_loop(self, poll_interval_seconds: int = 30) -> None:
        """Watches for open oracle rounds, computes scores, and submits."""
        # TODO: Poll for open rounds
        # TODO: Compute score using detection logic
        # TODO: Submit score
        pass
