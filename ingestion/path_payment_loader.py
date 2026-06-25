"""Path payment ingestion and hop decomposition for LedgerLens.

Stellar's path payment operations (``path_payment_strict_send`` and
``path_payment_strict_receive``) route a source asset to a destination asset
through a chain of intermediate order-book or AMM matches.  From the graph
engine's perspective each hop is an independent trade edge; ingesting the
operation as a single record leaves wash-trading rings that exploit path
routing invisible to Tarjan SCC analysis.

``PathPaymentDecomposer`` reconstructs individual hop ``Trade`` records from
the Horizon effects API.  The hops form a chain:

    hop 0: source_asset → path[0]
    hop 1: path[0]      → path[1]
    ...
    hop N: path[-1]     → destination_asset

Example (2-hop, XLM → BTC → USDC):
    effects = [
        TradeEffect(sold=XLM/10, bought=BTC/0.001),
        TradeEffect(sold=BTC/0.001, bought=USDC/95),
    ]
    decompose() → [
        Trade(base_asset=XLM, counter_asset=BTC, hop_index=0, ...),
        Trade(base_asset=BTC, counter_asset=USDC, hop_index=1, ...),
    ]
"""

import logging
import re
from datetime import datetime

import httpx

from config.settings import settings
from ingestion.data_models import (
    Asset,
    PathPayment,
    PathPaymentOperation,
    Trade,
    TradeEffect,
    TradeType,
)
from ingestion.http_client import AsyncHorizonClient, get_with_retry
from ingestion.operations_loader import _horizon_url, _parse_datetime, _parse_float

logger = logging.getLogger("ledgerlens.path_payment_loader")

PAGE_LIMIT = 200
MAX_PATH_HOPS = 8
PATH_PAYMENT_OPERATION_TYPES = ("path_payment_strict_send", "path_payment_strict_receive")

# Horizon operation IDs are large integers (numeric strings).
_OP_ID_RE = re.compile(r"^\d+$")

_MAX_AMOUNT = 10**15  # sanity bound (XLM has ~50B supply)


def _validate_op_id(op_id: str) -> bool:
    return bool(_OP_ID_RE.match(op_id))


def _asset_with_prefix(record: dict, prefix: str) -> Asset:
    key_prefix = f"{prefix}_" if prefix else ""
    asset_type = record.get(f"{key_prefix}asset_type")
    code = record.get(f"{key_prefix}asset_code")
    issuer = record.get(f"{key_prefix}asset_issuer")
    if asset_type == "native" or not code:
        return Asset(code="XLM", issuer=None)
    return Asset(code=code, issuer=issuer)


def _parse_path(record: dict) -> list[Asset]:
    raw_path = record.get("path") or []
    if len(raw_path) > MAX_PATH_HOPS:
        logger.warning(
            "Path payment %s has %d hops, exceeding bound of %d; truncating",
            record.get("id"), len(raw_path), MAX_PATH_HOPS,
        )
        raw_path = raw_path[:MAX_PATH_HOPS]
    hops = []
    for hop in raw_path:
        asset_type = hop.get("asset_type")
        code = hop.get("asset_code")
        issuer = hop.get("asset_issuer")
        if asset_type == "native" or not code:
            hops.append(Asset(code="XLM", issuer=None))
        else:
            hops.append(Asset(code=code, issuer=issuer))
    return hops


def _parse_path_payment(record: dict) -> PathPayment:
    return PathPayment(
        id=str(record.get("id") or ""),
        transaction_hash=str(record.get("transaction_hash") or ""),
        timestamp=_parse_datetime(record.get("created_at")),
        source_account=str(record.get("from") or record.get("source_account") or ""),
        destination_account=str(record.get("to") or ""),
        source_asset=_asset_with_prefix(record, "source"),
        destination_asset=_asset_with_prefix(record, ""),
        source_amount=_parse_float(record.get("source_amount")),
        destination_amount=_parse_float(record.get("amount")),
        path=_parse_path(record),
        strict_send=record.get("type") == "path_payment_strict_send",
    )


def _parse_path_payment_operation(record: dict) -> PathPaymentOperation | None:
    """Parse a Horizon operation record into a PathPaymentOperation, or None on error."""
    op_id = str(record.get("id") or "")
    if not _validate_op_id(op_id):
        logger.warning("Path payment operation has invalid id %r; skipping", op_id)
        return None
    op_type = record.get("type", "")
    if op_type not in PATH_PAYMENT_OPERATION_TYPES:
        return None
    try:
        from decimal import Decimal
        return PathPaymentOperation(
            id=op_id,
            paging_token=str(record.get("paging_token") or ""),
            transaction_hash=str(record.get("transaction_hash") or ""),
            ledger_close_time=_parse_datetime(record.get("created_at")),
            source_account=str(record.get("from") or record.get("source_account") or ""),
            destination_account=str(record.get("to") or ""),
            source_asset=_asset_with_prefix(record, "source"),
            destination_asset=_asset_with_prefix(record, ""),
            source_amount=Decimal(str(record.get("source_amount") or "0")),
            destination_amount=Decimal(str(record.get("amount") or "0")),
            path=_parse_path(record),
            operation_type=op_type,
        )
    except Exception as exc:
        logger.warning("Failed to parse path payment operation %s: %s", op_id, exc)
        return None


def _parse_trade_effect(record: dict) -> TradeEffect | None:
    try:
        from decimal import Decimal
        return TradeEffect(
            id=str(record.get("id") or ""),
            account=str(record.get("account") or ""),
            sold_asset_type=str(record.get("sold_asset_type") or "native"),
            sold_asset_code=record.get("sold_asset_code"),
            sold_asset_issuer=record.get("sold_asset_issuer"),
            sold_amount=Decimal(str(record.get("sold_amount") or "0")),
            bought_asset_type=str(record.get("bought_asset_type") or "native"),
            bought_asset_code=record.get("bought_asset_code"),
            bought_asset_issuer=record.get("bought_asset_issuer"),
            bought_amount=Decimal(str(record.get("bought_amount") or "0")),
        )
    except Exception as exc:
        logger.warning("Failed to parse trade effect %s: %s", record.get("id"), exc)
        return None


def _assets_match(a: Asset, b: Asset) -> bool:
    return a.code == b.code and a.issuer == b.issuer


class PathPaymentDecomposer:
    """Decomposes a PathPaymentOperation + its TradeEffects into per-hop Trade records.

    Each Trade is structurally identical to a regular order-book trade so the
    graph engine and feature pipeline need no changes.
    """

    def decompose(
        self,
        operation: PathPaymentOperation,
        effects: list[TradeEffect],
    ) -> list[Trade]:
        """Reconstruct individual hop trades from the effects list.

        The hops form a chain:
            hop 0: source_asset → path[0]
            hop 1: path[0]      → path[1]
            ...
            hop N: path[-1]     → destination_asset

        Args:
            operation: The path payment operation record.
            effects: Trade effects for this operation from Horizon /effects.

        Returns:
            List of Trade records (one per hop), or empty list if validation fails.
        """
        expected_hops = len(operation.path) + 1

        if not effects:
            # Approximate decomposition from operation data alone (no effects)
            return self._decompose_without_effects(operation)

        if len(effects) != expected_hops:
            logger.warning(
                "Path payment %s: expected %d effects for %d hops, got %d; skipping",
                operation.id, expected_hops, expected_hops, len(effects),
            )
            return []

        # Build the expected asset chain: source → path[0] → ... → destination
        asset_chain = [operation.source_asset] + list(operation.path) + [operation.destination_asset]

        trades: list[Trade] = []
        for i, effect in enumerate(effects):
            sold = effect.sold_asset
            bought = effect.bought_asset

            if not _assets_match(sold, asset_chain[i]):
                logger.warning(
                    "Path payment %s hop %d: expected sold_asset %s, got %s; skipping operation",
                    operation.id, i, asset_chain[i].pair_symbol, sold.pair_symbol,
                )
                return []

            sold_amt = float(effect.sold_amount)
            bought_amt = float(effect.bought_amount)

            if sold_amt <= 0 or bought_amt <= 0:
                logger.warning(
                    "Path payment %s hop %d: non-positive amounts (sold=%s bought=%s); skipping",
                    operation.id, i, sold_amt, bought_amt,
                )
                return []

            if sold_amt > _MAX_AMOUNT or bought_amt > _MAX_AMOUNT:
                logger.warning(
                    "Path payment %s hop %d: amount exceeds bound (%s); skipping",
                    operation.id, i, max(sold_amt, bought_amt),
                )
                return []

            hop_index = max(0, min(i, expected_hops - 1))  # bound to [0, len(path)]

            trades.append(Trade(
                id=f"{operation.id}-hop{hop_index}",
                ledger_close_time=operation.ledger_close_time,
                base_account=operation.source_account,
                counter_account=operation.destination_account,
                base_asset=sold,
                counter_asset=bought,
                base_amount=sold_amt,
                counter_amount=bought_amt,
                price=bought_amt / sold_amt,
                base_is_seller=True,
                trade_type=TradeType.ORDERBOOK,
                transaction_hash=operation.transaction_hash,
                path_payment_id=operation.id,
                hop_index=hop_index,
            ))

        return trades

    def _decompose_without_effects(self, operation: PathPaymentOperation) -> list[Trade]:
        """Approximate decomposition when effects are unavailable.

        Distributes source/destination amounts evenly across hops.
        """
        asset_chain = [operation.source_asset] + list(operation.path) + [operation.destination_asset]
        n_hops = len(asset_chain) - 1
        src = float(operation.source_amount)
        dst = float(operation.destination_amount)

        trades: list[Trade] = []
        for i in range(n_hops):
            sold_amt = src if i == 0 else dst
            bought_amt = dst if i == n_hops - 1 else dst
            if sold_amt <= 0 or bought_amt <= 0:
                continue
            trades.append(Trade(
                id=f"{operation.id}-hop{i}",
                ledger_close_time=operation.ledger_close_time,
                base_account=operation.source_account,
                counter_account=operation.destination_account,
                base_asset=asset_chain[i],
                counter_asset=asset_chain[i + 1],
                base_amount=sold_amt,
                counter_amount=bought_amt,
                price=bought_amt / sold_amt,
                base_is_seller=True,
                trade_type=TradeType.ORDERBOOK,
                transaction_hash=operation.transaction_hash,
                path_payment_id=operation.id,
                hop_index=i,
            ))
        return trades


class PathPaymentLoader:
    """Fetches path payment operations from Horizon and decomposes them into hop trades."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or settings.horizon_url
        self._decomposer = PathPaymentDecomposer()

    def _fetch_effects(self, op_id: str, client: httpx.Client) -> list[TradeEffect]:
        """Fetch trade effects for a single operation."""
        url = f"{self._base_url}/operations/{op_id}/effects"
        try:
            resp = get_with_retry(client, url, params={"limit": 200})
            records = resp.json().get("_embedded", {}).get("records", [])
            effects = []
            for r in records:
                if r.get("type") == "trade":
                    e = _parse_trade_effect(r)
                    if e:
                        effects.append(e)
            return effects
        except Exception as exc:
            logger.warning("Failed to fetch effects for operation %s: %s", op_id, exc)
            return []

    def load_hop_trades(
        self,
        account: str,
        since: datetime,
        limit: int = PAGE_LIMIT,
        fetch_effects: bool | None = None,
    ) -> list[Trade]:
        """Fetch path payments for `account` and decompose into hop Trade records.

        Args:
            account: Stellar account address.
            since: Only return trades at or after this datetime.
            limit: Horizon page size.
            fetch_effects: Override PATH_PAYMENT_FETCH_EFFECTS setting.

        Returns:
            Flat list of hop Trade records across all path payment operations.
        """
        if not settings.path_payment_loader_enabled:
            return []

        use_effects = fetch_effects if fetch_effects is not None else settings.path_payment_fetch_effects
        cutoff = _parse_datetime(since)
        url = _horizon_url(f"/accounts/{account}/operations")

        all_trades: list[Trade] = []
        with httpx.Client(timeout=30.0) as client:
            resp = get_with_retry(client, url, params={"limit": limit, "order": "desc"})
            records = resp.json().get("_embedded", {}).get("records", [])

            for record in records:
                if record.get("type") not in PATH_PAYMENT_OPERATION_TYPES:
                    continue
                op = _parse_path_payment_operation(record)
                if op is None or op.ledger_close_time < cutoff:
                    continue

                effects: list[TradeEffect] = []
                if use_effects:
                    effects = self._fetch_effects(op.id, client)

                trades = self._decomposer.decompose(op, effects)
                all_trades.extend(trades)

        return all_trades


# ---------------------------------------------------------------------------
# Legacy helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------


def load_path_payments(account: str, since: datetime, limit: int = PAGE_LIMIT) -> list[PathPayment]:
    """GET /accounts/{account}/operations filtered to path-payment operations since `since`."""
    cutoff = _parse_datetime(since)
    url = _horizon_url(f"/accounts/{account}/operations")
    with httpx.Client(timeout=30.0) as client:
        response = get_with_retry(client, url, params={"limit": limit, "order": "desc"})
        records = response.json().get("_embedded", {}).get("records", [])
    payments = [_parse_path_payment(r) for r in records if r.get("type") in PATH_PAYMENT_OPERATION_TYPES]
    return [p for p in payments if p.timestamp >= cutoff]


def load_path_payments_for_accounts(
    accounts: list[str],
    since: datetime,
    limit: int = PAGE_LIMIT,
) -> list[PathPayment]:
    payments: list[PathPayment] = []
    for account in accounts:
        payments.extend(load_path_payments(account, since, limit))
    return payments


async def async_load_path_payments(
    account: str,
    since: datetime,
    client: AsyncHorizonClient,
    limit: int = PAGE_LIMIT,
) -> list[PathPayment]:
    cutoff = _parse_datetime(since)
    data = await client.get(f"/accounts/{account}/operations", params={"limit": limit, "order": "desc"})
    records = data.get("_embedded", {}).get("records", [])
    payments = [_parse_path_payment(r) for r in records if r.get("type") in PATH_PAYMENT_OPERATION_TYPES]
    return [p for p in payments if p.timestamp >= cutoff]
