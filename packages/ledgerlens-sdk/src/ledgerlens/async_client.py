"""Asynchronous LedgerLens API client, for concurrent integrations (e.g.
scoring many wallets in parallel with `asyncio.gather`)."""

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


class AsyncLedgerLensClient:
    """Asynchronous client for the LedgerLens REST API.

    Example
    -------
        import asyncio
        from ledgerlens import AsyncLedgerLensClient

        async def main():
            async with AsyncLedgerLensClient(base_url="https://api.ledgerlens.io") as client:
                results = await asyncio.gather(*(client.get_score(w) for w in wallets))

        asyncio.run(main())
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        # Merge in even when an explicit `client=` was supplied, so api_key
        # always takes effect regardless of construction path.
        self._client.headers.update(build_headers(api_key))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncLedgerLensClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- internal helpers ---------------------------------------------------

    async def _get(self, path: str, params: dict | None = None):
        response = await self._client.get(path, params=clean_params(params))
        raise_for_status(response)
        return response.json()

    async def _post(self, path: str, json: dict | None = None):
        response = await self._client.post(path, json=json)
        raise_for_status(response)
        return response.json()

    async def _delete(self, path: str):
        response = await self._client.delete(path)
        raise_for_status(response)
        return response.json()

    # -- health ---------------------------------------------------------------

    async def health(self) -> HealthStatus:
        return HealthStatus.model_validate(await self._get("/health"))

    # -- scores -----------------------------------------------------------------

    async def list_scores(
        self,
        min_score: int = 0,
        limit: int = 100,
        offset: int = 0,
        benford_flag: bool | None = None,
        ml_flag: bool | None = None,
        sort_by: str = "score",
    ) -> list[RiskScore]:
        data = await self._get(
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

    async def get_score(self, wallet: str) -> WalletScoresResponse:
        """Latest score(s) for `wallet` across every asset pair it has traded."""
        return WalletScoresResponse.model_validate(await self._get(f"/scores/{wallet}"))

    async def explain_score(self, wallet: str, asset_pair: str) -> list[ShapContribution]:
        data = await self._get(f"/scores/{wallet}/explain", {"asset_pair": asset_pair})
        return [ShapContribution.model_validate(item) for item in data]

    async def get_counterfactual(
        self,
        wallet: str,
        asset_pair: str,
        n: int = 3,
        target_score: int | None = None,
    ) -> CounterfactualResponse:
        data = await self._get(
            f"/scores/{wallet}/counterfactual",
            {"asset_pair": asset_pair, "n": n, "target_score": target_score},
        )
        return CounterfactualResponse.model_validate(data)

    # -- alerts / rankings --------------------------------------------------------

    async def list_alerts(self, limit: int = 100, offset: int = 0, alert_type: str | None = None) -> list[dict]:
        return await self._get("/alerts", {"limit": limit, "offset": offset, "alert_type": alert_type})

    async def asset_risk_ranking(self) -> list[AssetRiskRanking]:
        data = await self._get("/assets/risk-ranking")
        return [AssetRiskRanking.model_validate(item) for item in data]

    async def list_rings(self) -> list[dict]:
        return await self._get("/rings")

    async def list_correlations(self) -> list[dict]:
        return await self._get("/correlations")

    async def pool_risk(self, pool_id: str) -> dict:
        return await self._get(f"/amm/pools/{pool_id}/risk")

    async def circular_path_payments(self, limit: int = 100, offset: int = 0) -> list[dict]:
        return await self._get("/path-payments/circular", {"limit": limit, "offset": offset})

    # -- webhooks (admin) -----------------------------------------------------------

    async def create_webhook(
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
        return WebhookCreated.model_validate(await self._post("/webhooks", body))

    async def list_webhooks(self) -> list[WebhookSubscriber]:
        data = await self._get("/webhooks")
        return [WebhookSubscriber.model_validate(item) for item in data]

    async def delete_webhook(self, subscriber_id: str) -> dict:
        return await self._delete(f"/webhooks/{subscriber_id}")

    # -- disputes ---------------------------------------------------------------------

    async def create_dispute(
        self, wallet: str, asset_pair: str, evidence_url: str | None = None
    ) -> DisputeCreated:
        body = {"wallet": wallet, "asset_pair": asset_pair, "evidence_url": evidence_url}
        return DisputeCreated.model_validate(await self._post("/disputes", body))

    async def get_dispute(self, dispute_id: str) -> Dispute:
        return Dispute.model_validate(await self._get(f"/disputes/{dispute_id}"))

    # -- feedback (admin) ---------------------------------------------------------------

    async def submit_feedback(self, wallet: str, asset_pair: str, ground_truth: int, scored_at: str) -> dict:
        body = {
            "wallet": wallet,
            "asset_pair": asset_pair,
            "ground_truth": ground_truth,
            "scored_at": scored_at,
        }
        return await self._post("/feedback", body)
