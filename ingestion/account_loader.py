"""Account metadata ingestion: funding source and account age.

Used by `detection.feature_engineering`'s wallet-graph features
(`funding_source_similarity_score`, `account_age_days`). Horizon does not
expose creation time directly on `/accounts/{id}`, so this walks the
account's oldest `create_account` operation.
"""

from datetime import datetime

import httpx

from config.settings import settings


def get_account_creation_info(account: str) -> dict:
    """Return `{"funding_source": str | None, "created_at": datetime | None}` for `account`.

    `funding_source` is the account that funded `account`'s `create_account`
    operation. Returns `None` values if the account has no such operation
    on record (e.g. it was created before Horizon's retention window).
    """
    url = f"{settings.horizon_url}/accounts/{account}/operations"
    params = {"order": "asc", "limit": 1}

    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
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
