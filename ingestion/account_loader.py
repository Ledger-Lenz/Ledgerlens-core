"""Account metadata ingestion: funding source and account age.

Used by `detection.feature_engineering`'s wallet-graph features
(`funding_source_similarity_score`, `account_age_days`). Horizon does not
expose creation time directly on `/accounts/{id}`, so this walks the
account's oldest `create_account` operation.
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from config.settings import settings
from ingestion.http_client import AsyncHorizonClient, get_with_retry


@dataclass
class AccountMetadata:
    account_id: str
    funding_source: str | None
    created_at: datetime | None
    home_domain: str | None
    num_signers: int
    low_threshold: int
    med_threshold: int
    high_threshold: int
    signer_keys: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def get_account_metadata_enriched(account: str) -> "AccountMetadata":
    """Fetch rich account metadata: home_domain, signers, thresholds, and creation info."""
    with httpx.Client(timeout=30.0) as client:
        acct_data = get_with_retry(client, f"{settings.horizon_url}/accounts/{account}").json()
        ops_records = get_with_retry(
            client,
            f"{settings.horizon_url}/accounts/{account}/operations",
            params={"order": "asc", "limit": 1},
        ).json().get("_embedded", {}).get("records", [])

    funding_source, created_at = None, None
    if ops_records and ops_records[0].get("type") == "create_account":
        rec = ops_records[0]
        funding_source = rec.get("funder")
        raw_ts = rec.get("created_at", "")
        if raw_ts:
            created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))

    thresholds = acct_data.get("thresholds", {})
    signers = acct_data.get("signers", [])
    return AccountMetadata(
        account_id=account,
        funding_source=funding_source,
        created_at=created_at,
        home_domain=acct_data.get("home_domain") or None,
        num_signers=len(signers),
        low_threshold=int(thresholds.get("low_threshold", 0)),
        med_threshold=int(thresholds.get("med_threshold", 0)),
        high_threshold=int(thresholds.get("high_threshold", 0)),
        signer_keys=[s["key"] for s in signers if "key" in s],
        fetched_at=datetime.now(timezone.utc),
    )


class AccountMetadataCache:
    """SQLite-backed TTL cache for AccountMetadata."""

    def __init__(self, ttl_seconds: int = 3600, db_path: str | None = None) -> None:
        self._ttl = ttl_seconds
        self._db_path = db_path or settings.db_path

    def get(self, account: str) -> "AccountMetadata | None":
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT account_id,funding_source,created_at,home_domain,"
                    "num_signers,low_threshold,med_threshold,high_threshold,"
                    "signer_keys_json,fetched_at FROM account_metadata_cache WHERE account_id=?",
                    (account,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row[9])
        if (datetime.now(timezone.utc) - fetched_at).total_seconds() > self._ttl:
            return None
        return AccountMetadata(
            account_id=row[0], funding_source=row[1],
            created_at=datetime.fromisoformat(row[2]) if row[2] else None,
            home_domain=row[3], num_signers=row[4], low_threshold=row[5],
            med_threshold=row[6], high_threshold=row[7],
            signer_keys=json.loads(row[8] or "[]"), fetched_at=fetched_at,
        )

    def set(self, metadata: "AccountMetadata") -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO account_metadata_cache
                   (account_id,funding_source,created_at,home_domain,num_signers,
                    low_threshold,med_threshold,high_threshold,signer_keys_json,fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET
                     funding_source=excluded.funding_source, created_at=excluded.created_at,
                     home_domain=excluded.home_domain, num_signers=excluded.num_signers,
                     low_threshold=excluded.low_threshold, med_threshold=excluded.med_threshold,
                     high_threshold=excluded.high_threshold,
                     signer_keys_json=excluded.signer_keys_json, fetched_at=excluded.fetched_at""",
                (
                    metadata.account_id, metadata.funding_source,
                    metadata.created_at.isoformat() if metadata.created_at else None,
                    metadata.home_domain, metadata.num_signers, metadata.low_threshold,
                    metadata.med_threshold, metadata.high_threshold,
                    json.dumps(metadata.signer_keys), metadata.fetched_at.isoformat(),
                ),
            )

    def load_all_enriched(self, accounts: list[str], concurrency: int = 10) -> "dict[str, AccountMetadata]":
        """Fetch metadata for all accounts in parallel, using cache for fresh entries."""
        result: dict[str, AccountMetadata] = {}
        missing = [a for a in accounts if not (cached := self.get(a)) or not result.update({a: cached})]  # type: ignore[func-returns-value]
        # Re-do cleanly:
        result = {}
        missing = []
        for a in accounts:
            cached = self.get(a)
            if cached:
                result[a] = cached
            else:
                missing.append(a)
        if missing:
            fetched = asyncio.run(self._fetch_parallel(missing, concurrency))
            for a, meta in fetched.items():
                self.set(meta)
                result[a] = meta
        return result

    async def _fetch_parallel(self, accounts: list[str], concurrency: int) -> "dict[str, AccountMetadata]":
        sem = asyncio.Semaphore(concurrency)

        async def _one(account: str) -> tuple[str, "AccountMetadata"]:
            async with sem:
                loop = asyncio.get_event_loop()
                return account, await loop.run_in_executor(None, get_account_metadata_enriched, account)

        pairs = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
        return {a: m for a, m in pairs if not isinstance(m, Exception)}


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
