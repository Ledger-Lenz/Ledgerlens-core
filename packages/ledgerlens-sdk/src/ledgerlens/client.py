"""Synchronous LedgerLens API client."""

from __future__ import annotations

import httpx

from ._base import DEFAULT_TIMEOUT, build_headers, clean_params, raise_for_status
from .models import (
    AssetRiskRanking,
    CounterfactualResponse,
    Dispute,
    DisputeCreated,
    HealthStatus,
    RiskScore,
    ShapContribution,
    WalletScoresResponse,
    WebhookCreated,
    WebhookSubscriber,
)


class LedgerLensClient:
    """Synchronous client for the LedgerLens REST API.

    Example
    -------
        from ledgerlens import LedgerLensClient

        with LedgerLensClient(base_url="https://api.ledgerlens.io", api_key="...") as client:
            result = client.get_score("GABC...")
            print(result.scores)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        # Merge in even when an explicit `client=` was supplied, so api_key
        # always takes effect regardless of construction path.
        self._client.headers.update(build_headers(api_key))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LedgerLensClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internal helpers ---------------------------------------------------

    def _get(self, path: str, params: dict | None = None):
        response = self._client.get(path, params=clean_params(params))
        raise_for_status(response)
        return response.json()

    def _post(self, path: str, json: dict | None = None):
        response = self._client.post(path, json=json)
        raise_for_status(response)
        return response.json()

    def _delete(self, path: str):
        response = self._client.delete(path)
        raise_for_status(response)
        return response.json()

    # -- health ---------------------------------------------------------------

    def health(self) -> HealthStatus:
        return HealthStatus.model_validate(self._get("/health"))

    # -- scores -----------------------------------------------------------------

    def list_scores(
        self,
        min_score: int = 0,
        limit: int = 100,
        offset: int = 0,
        benford_flag: bool | None = None,
        ml_flag: bool | None = None,
        sort_by: str = "score",
    ) -> list[RiskScore]:
        data = self._get(
            "/scores",
            {
                "min_score": min_score,
                "limit": limit,
                "offset": offset,
                "benford_flag": benford_flag,
                "ml_flag": ml_flag,
                "sort_by": sort_by,
            },
        )
        return [RiskScore.model_validate(item) for item in data]

    def get_score(self, wallet: str) -> WalletScoresResponse:
        """Latest score(s) for `wallet` across every asset pair it has traded."""
        return WalletScoresResponse.model_validate(self._get(f"/scores/{wallet}"))

    def explain_score(self, wallet: str, asset_pair: str) -> list[ShapContribution]:
        data = self._get(f"/scores/{wallet}/explain", {"asset_pair": asset_pair})
        return [ShapContribution.model_validate(item) for item in data]

    def get_counterfactual(
        self,
        wallet: str,
        asset_pair: str,
        n: int = 3,
        target_score: int | None = None,
    ) -> CounterfactualResponse:
        data = self._get(
            f"/scores/{wallet}/counterfactual",
            {"asset_pair": asset_pair, "n": n, "target_score": target_score},
        )
        return CounterfactualResponse.model_validate(data)

    # -- alerts / rankings --------------------------------------------------------

    def list_alerts(self, limit: int = 100, offset: int = 0, alert_type: str | None = None) -> list[dict]:
        return self._get("/alerts", {"limit": limit, "offset": offset, "alert_type": alert_type})

    def asset_risk_ranking(self) -> list[AssetRiskRanking]:
        data = self._get("/assets/risk-ranking")
        return [AssetRiskRanking.model_validate(item) for item in data]

    def list_rings(self) -> list[dict]:
        return self._get("/rings")

    def list_correlations(self) -> list[dict]:
        return self._get("/correlations")

    def pool_risk(self, pool_id: str) -> dict:
        return self._get(f"/amm/pools/{pool_id}/risk")

    def circular_path_payments(self, limit: int = 100, offset: int = 0) -> list[dict]:
        return self._get("/path-payments/circular", {"limit": limit, "offset": offset})

    # -- webhooks (admin) -----------------------------------------------------------

    def create_webhook(
        self,
        url: str,
        secret: str,
        min_score: int = 70,
        wallet_filter: str | None = None,
        asset_pair_filter: str | None = None,
    ) -> WebhookCreated:
        body = {
            "url": url,
            "secret": secret,
            "min_score": min_score,
            "wallet_filter": wallet_filter,
            "asset_pair_filter": asset_pair_filter,
        }
        return WebhookCreated.model_validate(self._post("/webhooks", body))

    def list_webhooks(self) -> list[WebhookSubscriber]:
        data = self._get("/webhooks")
        return [WebhookSubscriber.model_validate(item) for item in data]

    def delete_webhook(self, subscriber_id: str) -> dict:
        return self._delete(f"/webhooks/{subscriber_id}")

    # -- disputes ---------------------------------------------------------------------

    def create_dispute(self, wallet: str, asset_pair: str, evidence_url: str | None = None) -> DisputeCreated:
        body = {"wallet": wallet, "asset_pair": asset_pair, "evidence_url": evidence_url}
        return DisputeCreated.model_validate(self._post("/disputes", body))

    def get_dispute(self, dispute_id: str) -> Dispute:
        return Dispute.model_validate(self._get(f"/disputes/{dispute_id}"))

    # -- feedback (admin) ---------------------------------------------------------------

    def submit_feedback(self, wallet: str, asset_pair: str, ground_truth: int, scored_at: str) -> dict:
        body = {
            "wallet": wallet,
            "asset_pair": asset_pair,
            "ground_truth": ground_truth,
            "scored_at": scored_at,
        }
        return self._post("/feedback", body)
