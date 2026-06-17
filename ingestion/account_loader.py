"""Account metadata ingestion: funding source and account age.

Used by `detection.feature_engineering`'s wallet-graph features
(`funding_source_similarity_score`, `account_age_days`). Horizon does not
expose creation time directly on `/accounts/{id}`, so this walks the
account's oldest `create_account` operation.
"""

import asyncio
from datetime import datetime

import httpx

from config.settings import settings
from ingestion.http_client import AsyncHorizonClient, get_with_retry


def get_account_creation_info(account: str) -> dict:
    """Return `{"funding_source": str | None, "created_at": datetime | None}` for `account`.

    `funding_source` is the account that funded `account`'s `create_account`
    operation. Returns `None` values if the account has no such operation
    on record (e.g. it was created before Horizon's retention window).
    """
    url = f"{settings.horizon_url}/accounts/{account}/operations"
    params = {"order": "asc", "limit": 1}

    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params=params)
        records = response.json()["_embedded"]["records"]

    if not records or records[0]["type"] != "create_account":
        return {"funding_source": None, "created_at": None}

    record = records[0]
    return {
        "funding_source": record["funder"],
        "created_at": datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
    }


def load_account_metadata(accounts: list[str]) -> dict[str, dict]:
    """Return `{account: {"funding_source":..., "created_at":...}}` for each account in `accounts`."""
    return {account: get_account_creation_info(account) for account in accounts}


def _parse_creation_info(data: dict) -> dict:
    records = data.get("_embedded", {}).get("records", [])
    if not records or records[0].get("type") != "create_account":
        return {"funding_source": None, "created_at": None}
    record = records[0]
    return {
        "funding_source": record["funder"],
        "created_at": datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
    }


async def _async_get_account_creation_info(account: str, client: AsyncHorizonClient) -> dict:
    data = await client.get(
        f"/accounts/{account}/operations",
        params={"order": "asc", "limit": 1},
    )
    return _parse_creation_info(data)


async def async_load_account_metadata(
    accounts: list[str],
    client: AsyncHorizonClient,
) -> dict[str, dict]:
    """Fetch creation metadata for all `accounts` concurrently.

    Concurrency is bounded by the semaphore inside `client`. Returns the same
    `{account: {"funding_source":..., "created_at":...}}` mapping as the
    synchronous `load_account_metadata`.
    """
    tasks = [_async_get_account_creation_info(a, client) for a in accounts]
    results = await asyncio.gather(*tasks)
    return dict(zip(accounts, results))
