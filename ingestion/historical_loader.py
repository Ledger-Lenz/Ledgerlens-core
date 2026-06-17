"""Bulk historical trade ingestion from Horizon, used for model training data.

Paginates through `/trades` (or `/order_book` history for a given asset
pair) and returns a pandas DataFrame ready for `detection.feature_engineering`.
Persisted output is handed off to the ledgerlens-data repo for storage.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from config.settings import settings
from ingestion.horizon_streamer import _parse_trade
from ingestion.http_client import AsyncHorizonClient, get_with_retry

PAGE_LIMIT = 200


def load_historical_trades(
    base_asset: str | None = None,
    counter_asset: str | None = None,
    lookback_days: int | None = None,
) -> pd.DataFrame:
    """Fetch historical trades from Horizon and return them as a DataFrame.

    Parameters
    ----------
    base_asset, counter_asset:
        Optional asset filters in `CODE:ISSUER` form (omit issuer for XLM).
    lookback_days:
        How far back to page; defaults to `settings.trade_history_lookback_days`.
    """
    lookback_days = lookback_days or settings.trade_history_lookback_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    params: dict[str, str | int] = {"limit": PAGE_LIMIT, "order": "desc"}
    if base_asset:
        params.update(_asset_params("base", base_asset))
    if counter_asset:
        params.update(_asset_params("counter", counter_asset))

    records: list[dict] = []
    url = f"{settings.horizon_url}/trades"

    with httpx.Client(timeout=30.0) as client:
        while url:
            response = get_with_retry(client, url, params=params)
            payload = response.json()
            page_records = payload["_embedded"]["records"]
            if not page_records:
                break

            for record in page_records:
                close_time = datetime.fromisoformat(record["ledger_close_time"].replace("Z", "+00:00"))
                if close_time < cutoff:
                    return _to_dataframe(records)
                records.append(record)

            url = payload["_links"]["next"]["href"]
            params = {}  # next link already encodes query params

    return _to_dataframe(records)


def _asset_params(prefix: str, asset: str) -> dict[str, str]:
    """Build Horizon `{prefix}_asset_type`/`_code`/`_issuer` query params for `asset`.

    `asset` is `CODE:ISSUER` for credit assets, or `"XLM"`/`"native"` for the
    native asset (which takes no code/issuer params on Horizon).
    """
    if ":" in asset:
        code, issuer = asset.split(":", 1)
        asset_type = "credit_alphanum12" if len(code) > 4 else "credit_alphanum4"
        return {f"{prefix}_asset_type": asset_type, f"{prefix}_asset_code": code, f"{prefix}_asset_issuer": issuer}
    return {f"{prefix}_asset_type": "native"}


def _to_dataframe(records: list[dict]) -> pd.DataFrame:
    trades = [_parse_trade(r).model_dump() for r in records]
    return pd.DataFrame(trades)


async def async_load_historical_trades(
    base_asset: str | None = None,
    counter_asset: str | None = None,
    lookback_days: int | None = None,
    client: AsyncHorizonClient | None = None,
) -> pd.DataFrame:
    """Async variant of `load_historical_trades` using `AsyncHorizonClient`.

    Paginates through Horizon `/trades` concurrently with other in-flight
    requests; the semaphore inside `client` prevents overloading Horizon.
    """
    assert client is not None, "client is required for async_load_historical_trades"
    lookback_days = lookback_days or settings.trade_history_lookback_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    params: dict[str, str | int] = {"limit": PAGE_LIMIT, "order": "desc"}
    if base_asset:
        params.update(_asset_params("base", base_asset))
    if counter_asset:
        params.update(_asset_params("counter", counter_asset))

    records: list[dict] = []
    path: str | None = "/trades"
    request_params: dict | None = params

    while path is not None:
        payload = await client.get(path, params=request_params)
        page_records = payload.get("_embedded", {}).get("records", [])
        if not page_records:
            break

        for record in page_records:
            close_time = datetime.fromisoformat(record["ledger_close_time"].replace("Z", "+00:00"))
            if close_time < cutoff:
                return _to_dataframe(records)
            records.append(record)

        path = payload.get("_links", {}).get("next", {}).get("href") or None
        request_params = None  # next URL already encodes all query params

    return _to_dataframe(records)


if __name__ == "__main__":
    df = load_historical_trades()
    print(f"Loaded {len(df)} trades")
