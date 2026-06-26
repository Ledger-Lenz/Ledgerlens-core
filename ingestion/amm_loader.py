"""AMM liquidity pool ingestion from Horizon (CAP-38).

Mirrors `ingestion/operations_loader.py`'s structure: all Horizon calls go
through `AsyncHorizonClient` / `get_with_retry` rather than raw `httpx`, and
both sync and async entry points are provided.
"""

import logging
import threading
import time
from datetime import datetime
from decimal import Decimal

import httpx

from ingestion.data_models import (
    Asset,
    LiquidityPool,
    LiquidityPoolEvent,
    LiquidityPoolEventType,
    PoolReserve,
    Trade,
    TradeType,
)
from ingestion.http_client import AsyncHorizonClient, get_with_retry
from ingestion.operations_loader import _horizon_url, _parse_datetime, _parse_float

logger = logging.getLogger(__name__)

PAGE_LIMIT = 200


def _reserve_asset(code: str) -> Asset:
    if code in (None, "", "native"):
        return Asset(code="XLM", issuer=None)
    if ":" in code:
        asset_code, issuer = code.split(":", 1)
        return Asset(code=asset_code, issuer=issuer)
    return Asset(code=code, issuer=None)


def _parse_pool(record: dict) -> LiquidityPool:
    reserves = [
        (_reserve_asset(r.get("asset")), _parse_float(r.get("amount")))
        for r in record.get("reserves", [])
    ]
    return LiquidityPool(
        id=str(record.get("id") or ""),
        fee_bp=int(_parse_float(record.get("fee_bp"))),
        total_shares=_parse_float(record.get("total_shares")),
        reserves=reserves,
    )


def _price(record: dict) -> float:
    price = record.get("price")
    if isinstance(price, dict):
        try:
            return float(price["n"]) / float(price["d"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return 0.0
    return _parse_float(price)


def _parse_pool_trade(record: dict, pool_id: str) -> Trade:
    base_asset = Asset(code=record.get("base_asset_code", "XLM"), issuer=record.get("base_asset_issuer"))
    counter_asset = Asset(code=record.get("counter_asset_code", "XLM"), issuer=record.get("counter_asset_issuer"))
    return Trade(
        id=str(record.get("id") or ""),
        ledger_close_time=_parse_datetime(record.get("ledger_close_time")),
        base_account=str(record.get("base_account") or ""),
        counter_account=None,
        base_asset=base_asset,
        counter_asset=counter_asset,
        base_amount=_parse_float(record.get("base_amount")),
        counter_amount=_parse_float(record.get("counter_amount")),
        price=_price(record),
        base_is_seller=bool(record.get("base_is_seller", False)),
        trade_type=TradeType.LIQUIDITY_POOL,
        liquidity_pool_id=pool_id,
    )


def load_liquidity_pools(limit: int = PAGE_LIMIT) -> list[LiquidityPool]:
    """GET /liquidity_pools — current pool reserves and share counts."""
    url = _horizon_url("/liquidity_pools")
    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params={"limit": limit})
        records = response.json().get("_embedded", {}).get("records", [])
    return [_parse_pool(r) for r in records]


def load_liquidity_pool_trades(pool_id: str, since: datetime, limit: int = PAGE_LIMIT) -> list[Trade]:
    """GET /liquidity_pools/{pool_id}/trades, mapped to `Trade` records.

    Each trade has `trade_type=LIQUIDITY_POOL`, `liquidity_pool_id` set, and
    `counter_account=None` — the pool is the counterparty, not a wallet.
    """
    cutoff = _parse_datetime(since)
    url = _horizon_url(f"/liquidity_pools/{pool_id}/trades")
    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params={"limit": limit, "order": "desc"})
        records = response.json().get("_embedded", {}).get("records", [])
    trades = [_parse_pool_trade(r, pool_id) for r in records]
    return [t for t in trades if t.ledger_close_time >= cutoff]


async def async_load_liquidity_pool_trades(
    pool_id: str,
    since: datetime,
    client: AsyncHorizonClient,
    limit: int = PAGE_LIMIT,
) -> list[Trade]:
    """Async variant of `load_liquidity_pool_trades` using `AsyncHorizonClient`."""
    cutoff = _parse_datetime(since)
    data = await client.get(f"/liquidity_pools/{pool_id}/trades", params={"limit": limit, "order": "desc"})
    records = data.get("_embedded", {}).get("records", [])
    trades = [_parse_pool_trade(r, pool_id) for r in records]
    return [t for t in trades if t.ledger_close_time >= cutoff]


# ---------------------------------------------------------------------------
# LiquidityPoolEvent parsing
# ---------------------------------------------------------------------------

def _parse_pool_reserve(r: dict) -> PoolReserve:
    return PoolReserve(
        asset=str(r.get("asset") or "native"),
        amount=Decimal(str(r.get("amount") or "0")),
    )


def _parse_pool_event(record: dict, pool_id: str) -> LiquidityPoolEvent | None:
    op_type = record.get("type", "")
    if op_type == "liquidity_pool_deposit":
        event_type = LiquidityPoolEventType.DEPOSIT
        reserves_deposited = [_parse_pool_reserve(r) for r in record.get("reserves_deposited", [])]
        reserves_received = None
    elif op_type == "liquidity_pool_withdraw":
        event_type = LiquidityPoolEventType.WITHDRAW
        reserves_deposited = None
        reserves_received = [_parse_pool_reserve(r) for r in record.get("reserves_received", [])]
    else:
        return None

    shares_raw = record.get("shares_received") or record.get("shares") or "0"
    min_price_raw = record.get("min_price")
    max_price_raw = record.get("max_price")

    return LiquidityPoolEvent(
        id=str(record.get("id") or ""),
        paging_token=str(record.get("paging_token") or ""),
        transaction_hash=str(record.get("transaction_hash") or ""),
        ledger_close_time=_parse_datetime(record.get("created_at") or record.get("ledger_close_time")),
        pool_id=pool_id,
        account=str(record.get("source_account") or record.get("account") or ""),
        event_type=event_type,
        reserves_deposited=reserves_deposited,
        reserves_received=reserves_received,
        shares_amount=Decimal(str(shares_raw)),
        min_price=Decimal(str(min_price_raw)) if min_price_raw else None,
        max_price=Decimal(str(max_price_raw)) if max_price_raw else None,
    )


# ---------------------------------------------------------------------------
# AMMLoader
# ---------------------------------------------------------------------------

class AMMLoader:
    """Ingests deposit, withdrawal, and swap events from a single AMM pool."""

    def __init__(self, client: httpx.Client | None = None, limit: int = PAGE_LIMIT) -> None:
        self._client = client
        self._limit = limit

    def load_pool_events(self, pool_id: str, since: datetime) -> list[LiquidityPoolEvent]:
        """GET /liquidity_pools/{pool_id}/operations filtered to deposit/withdraw."""
        cutoff = _parse_datetime(since)
        url = _horizon_url(f"/liquidity_pools/{pool_id}/operations")
        params = {"limit": self._limit, "order": "desc"}

        def _fetch(client: httpx.Client) -> list[LiquidityPoolEvent]:
            response = get_with_retry(client, url, params=params)
            records = response.json().get("_embedded", {}).get("records", [])
            events = [_parse_pool_event(r, pool_id) for r in records]
            events = [e for e in events if e is not None and e.ledger_close_time >= cutoff]
            return events

        if self._client is not None:
            return _fetch(self._client)
        with httpx.Client(timeout=30.0) as client:
            return _fetch(client)

    def load_pool_trades(self, pool_id: str, since: datetime) -> list[Trade]:
        """GET /liquidity_pools/{pool_id}/trades mapped to Trade records."""
        return load_liquidity_pool_trades(pool_id, since, limit=self._limit)

    def load_all(self, pool_id: str, since: datetime) -> tuple[list[LiquidityPoolEvent], list[Trade]]:
        """Return (events, trades) for a pool since a given datetime."""
        return self.load_pool_events(pool_id, since), self.load_pool_trades(pool_id, since)


# ---------------------------------------------------------------------------
# AMMPoolRegistry
# ---------------------------------------------------------------------------

_DEFAULT_MIN_TVL_XLM = 1000.0
_DEFAULT_REFRESH_SECONDS = 3600


class AMMPoolRegistry:
    """Fetches and caches the list of active pools from /liquidity_pools.

    Pools with total reserves below `min_tvl_xlm` (expressed as a total XLM
    equivalent of both reserves) are excluded to avoid ingesting dust pools.
    Refresh happens at most once per `refresh_interval_seconds`.
    """

    def __init__(
        self,
        min_tvl_xlm: float = _DEFAULT_MIN_TVL_XLM,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_SECONDS,
        limit: int = PAGE_LIMIT,
    ) -> None:
        self.min_tvl_xlm = min_tvl_xlm
        self.refresh_interval_seconds = refresh_interval_seconds
        self._limit = limit
        self._pools: list[LiquidityPool] = []
        self._last_refresh: float = 0.0
        self._lock = threading.Lock()

    def _needs_refresh(self) -> bool:
        return (time.monotonic() - self._last_refresh) >= self.refresh_interval_seconds

    def _pool_tvl(self, pool: LiquidityPool) -> float:
        return sum(amount for _, amount in pool.reserves)

    def _fetch_pools(self) -> list[LiquidityPool]:
        raw = load_liquidity_pools(limit=self._limit)
        return [p for p in raw if self._pool_tvl(p) >= self.min_tvl_xlm]

    def get_pools(self, force_refresh: bool = False) -> list[LiquidityPool]:
        """Return the cached list of active pools, refreshing if stale."""
        with self._lock:
            if force_refresh or self._needs_refresh():
                try:
                    self._pools = self._fetch_pools()
                    self._last_refresh = time.monotonic()
                    logger.info("AMMPoolRegistry refreshed: %d pools above TVL threshold", len(self._pools))
                except Exception:
                    logger.exception("AMMPoolRegistry refresh failed; using cached data")
            return list(self._pools)

    def get_pool_ids(self, force_refresh: bool = False) -> list[str]:
        return [p.id for p in self.get_pools(force_refresh=force_refresh)]
