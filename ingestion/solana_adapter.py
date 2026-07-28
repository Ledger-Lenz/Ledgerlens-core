"""Solana SPL token trade ingestion adapter.

Fetches SPL token swap events for Serum/OpenBook DEX activity via the
Solana JSON-RPC API (getSignaturesForAddress + getTransaction).  Trades
are mapped to the canonical Trade dataclass so they flow through the
same feature-engineering and detection pipeline as Stellar SDEX trades.

Wormhole bridge VAA parsing links a Stellar G... address to its paired
Solana pubkey by scanning the PostedVAA accounts on-chain.

Configuration
-------------
SOLANA_RPC_URL : str, default https://api.mainnet-beta.solana.com
    Set to https://api.devnet.solana.com for devnet.
SOLANA_REQUEST_TIMEOUT : float, default 30.0
    HTTP timeout for RPC calls.
"""

from __future__ import annotations

import base64
import binascii
import logging
import math
import os
import struct
from datetime import datetime, timezone
from typing import Any

import httpx

from ingestion.data_models import Asset, Trade, TradeType

logger = logging.getLogger("ledgerlens.solana_adapter")

__all__ = ["SolanaAdapter"]

_DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

# Serum/OpenBook DEX v3 program ID on mainnet-beta
SERUM_DEX_V3 = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OPENBOOK_DEX = "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"

# Wormhole core bridge program on mainnet-beta
WORMHOLE_CORE = "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth"

_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

SOURCE_LABEL = "solana"

# Balance deltas at or below this magnitude are treated as noise/rounding.
_AMOUNT_EPSILON = 1e-9


def _rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", _DEFAULT_RPC)


def _timeout() -> float:
    return float(os.environ.get("SOLANA_REQUEST_TIMEOUT", "30.0"))


def _post(
    method: str,
    params: list[Any],
    client: httpx.Client,
    rpc_url: str | None = None,
) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = client.post(rpc_url or _rpc_url(), json=payload, timeout=_timeout())
    resp.raise_for_status()
    data = resp.json()
    if data.get("error") is not None:
        raise RuntimeError(f"Solana RPC error: {data['error']}")
    return data.get("result")


def _get_signatures(
    address: str,
    client: httpx.Client,
    limit: int = 100,
    before: str | None = None,
    rpc_url: str | None = None,
) -> list[dict]:
    options: dict[str, Any] = {"limit": limit, "commitment": "finalized"}
    if before:
        options["before"] = before
    result = _post("getSignaturesForAddress", [address, options], client, rpc_url)
    return result or []


def _get_transaction(
    sig: str,
    client: httpx.Client,
    rpc_url: str | None = None,
) -> dict | None:
    return _post(
        "getTransaction",
        [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}],
        client,
        rpc_url,
    )


def _is_dex_transaction(tx: dict) -> bool:
    account_keys: list[str] = (
        tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    )
    return SERUM_DEX_V3 in account_keys or OPENBOOK_DEX in account_keys


def _extract_spl_token_changes(
    tx: dict,
) -> list[tuple[str, str, float]]:
    """Return ``(owner_pubkey, mint, amount_change)`` for each SPL balance change.

    pre_map: dict[tuple[int, str], float] = {}
    for b in pre:
        idx = b.get("accountIndex", -1)
        mint = b.get("mint", "")
        amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
        pre_map[(idx, mint)] = amt

    changes: list[tuple[str, str, float]] = []
    for key in sorted(pre_map.keys() | post_map.keys()):
        pre_entry = pre_map.get(key)
        post_entry = post_map.get(key)
        delta = _amount(post_entry or {}) - _amount(pre_entry or {})
        if not math.isfinite(delta) or abs(delta) <= _AMOUNT_EPSILON:
            continue
        # ``owner`` may be absent from one side; prefer the post-state owner.
        owner = (post_entry or {}).get("owner") or (pre_entry or {}).get("owner") or ""
        if not owner:
            continue
        changes.append((owner, key[1], delta))

    return changes


def _tx_to_trade(tx: dict, sig: str) -> Trade | None:
    """Map a Solana DEX transaction to a canonical :class:`Trade`, or ``None``.

    Returns ``None`` (never raises) whenever the transaction cannot be expressed
    as a well-formed trade: missing/invalid ``blockTime``, fewer than two SPL
    balance changes, or a one-sided (buy-only / sell-only) transfer.
    """
    block_time = tx.get("blockTime")
    if block_time is None or isinstance(block_time, bool):
        return None
    try:
        ts = datetime.fromtimestamp(int(block_time), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None

    changes = _extract_spl_token_changes(tx)
    if len(changes) < 2:
        return None

    sells = [(owner, mint, -delta) for owner, mint, delta in changes if delta < 0]
    buys = [(owner, mint, delta) for owner, mint, delta in changes if delta > 0]
    if not sells or not buys:
        return None

    # Base leg: the first (deterministically ordered) outgoing transfer.
    base_owner, base_mint, base_amount = sells[0]

    # Counter leg: what the base owner received in exchange.  Prefer the base
    # owner's own incoming leg — pairing against an arbitrary ``buys[0]`` used
    # to pick the *counterparty's* receipt of the base asset, producing a
    # nonsensical A/A "trade" whose price depended on RPC response ordering.
    counter_candidates = [b for b in buys if b[1] != base_mint]
    if not counter_candidates:
        return None
    counter_leg = next(
        (b for b in counter_candidates if b[0] == base_owner), counter_candidates[0]
    )
    _, counter_mint, counter_amount = counter_leg

    # The counterparty is whoever gave up the counter asset; fall back to the
    # receiving owner when no distinct seller of that mint is visible.
    counter_owner = next(
        (owner for owner, mint, _ in sells if mint == counter_mint and owner != base_owner),
        counter_leg[0],
    )

    # Trade enforces strictly-positive amounts/price; bail out instead of
    # letting a pydantic ValidationError escape into the ingest loop.
    if base_amount <= 0 or counter_amount <= 0:
        return None
    price = counter_amount / base_amount
    if price <= 0 or not math.isfinite(price):
        return None

    return Trade(
        id=sig,
        ledger_close_time=ts,
        base_account=base_owner,
        counter_account=counter_owner if counter_owner != base_owner else None,
        base_asset=Asset(code=base_mint[:8], issuer=base_mint),
        counter_asset=Asset(code=counter_mint[:8], issuer=counter_mint),
        base_amount=base_amount,
        counter_amount=counter_amount,
        price=price,
        base_is_seller=True,
        trade_type=TradeType.ORDERBOOK,
        transaction_hash=sig,
        source=SOURCE_LABEL,
    )


class SolanaAdapter:
    """Ingests SPL token swap events from Serum/OpenBook DEX for a Solana address.

    Parameters
    ----------
    rpc_url:
        Overrides the SOLANA_RPC_URL environment variable when provided.
    dedup_store:
        Optional IdempotencyKeyStore instance. If not provided and settings.ingestion_dedup_enabled is True,
        a default one will be created.
    """

    def __init__(
        self,
        rpc_url: str | None = None,
        dedup_store: IdempotencyKeyStore | None = None,
    ) -> None:
        from config.settings import settings
        # Imported at runtime inside __init__ to avoid a circular import at
        # module load time between ingestion.solana_adapter and ingestion.dedup.
        from ingestion.dedup import IdempotencyKeyStore

        self.dedup_store = dedup_store or (
            IdempotencyKeyStore(
                db_path=settings.db_path,
                replay_window_seconds=settings.idempotency_replay_window_seconds,
            )
            if settings.ingestion_dedup_enabled
            else None
        )

    def _effective_rpc_url(self) -> str:
        """Explicit constructor argument wins; otherwise fall back to the env."""
        return self.rpc_url or _rpc_url()

    def _accept_trade(self, trade: Trade, sig: str, address: str) -> bool:
        """Return True when ``trade`` is new; records it in the dedup store."""
        if self.dedup_store is None:
            return True

        from ingestion.dedup import DedupResult

        key = self.dedup_store.compute_key(
            SOURCE_LABEL, signature=sig, instruction_index=0
        )
        metadata = {"signature": sig, "instruction_index": 0, "wallet": address}
        result = self.dedup_store.is_duplicate(
            key,
            timestamp=trade.ledger_close_time,
            source=SOURCE_LABEL,
            metadata=metadata,
        )
        if result is DedupResult.DUPLICATE:
            logger.debug("Skipping duplicate Solana trade %s", key[:16])
            return False
        if result is DedupResult.REPLAY_REJECTED:
            logger.warning("Rejecting replay Solana trade %s", key[:16])
            return False

        self.dedup_store.mark_seen(key, source=SOURCE_LABEL, metadata=metadata)
        return True

    def ingest(
        self,
        address: str,
        limit: int = 100,
        before_signature: str | None = None,
        client: httpx.Client | None = None,
    ) -> list[Trade]:
        """Fetch SPL swap events for ``address`` and return canonical Trade records.

        ``client`` may be supplied to reuse an existing (or mock-transport)
        httpx.Client; when omitted a short-lived client is created and closed.
        """
        trades: list[Trade] = []
        rpc_url = self._effective_rpc_url()
        own_client = client is None
        client = client or httpx.Client()
        try:
            sigs = _get_signatures(
                address, client, limit=limit, before=before_signature, rpc_url=rpc_url
            )
            for sig_info in sigs:
                sig = sig_info.get("signature", "")
                if not sig:
                    continue
                try:
                    tx = _get_transaction(sig, client, rpc_url=rpc_url)
                    if not tx or not _is_dex_transaction(tx):
                        continue
                    trade = _tx_to_trade(tx, sig)
                    if trade is not None and self._accept_trade(trade, sig, address):
                        trades.append(trade)
                except Exception:
                    logger.warning("Failed to process Solana tx %s", sig, exc_info=True)
        finally:
            if own_client:
                client.close()

        logger.info("solana.ingest address=%s trades=%d", address, len(trades))
        return trades

    def resolve_stellar_link(
        self,
        solana_address: str,
        client: httpx.Client | None = None,
    ) -> str | None:
        """Return the Stellar G... address linked via Wormhole VAA, or None."""
        own_client = client is None
        if own_client:
            client = httpx.Client()
        try:
            return self._parse_wormhole_vaa(solana_address, client)
        finally:
            if own_client:
                client.close()

    def _parse_wormhole_vaa(
        self,
        solana_address: str,
        client: httpx.Client,
    ) -> str | None:
        rpc_url = self._effective_rpc_url()
        sigs = _get_signatures(solana_address, client, limit=50, rpc_url=rpc_url)
        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            if not sig:
                continue
            try:
                tx = _get_transaction(sig, client, rpc_url=rpc_url)
                if not tx:
                    continue
                stellar_addr = _extract_stellar_address_from_vaa(tx)
                if stellar_addr:
                    logger.info(
                        "wormhole.vaa_link solana=%s stellar=%s",
                        solana_address,
                        stellar_addr,
                    )
                    return stellar_addr
            except Exception:
                logger.debug("VAA parse failed for tx %s", sig, exc_info=True)
        return None


def _extract_stellar_address_from_vaa(tx: dict) -> str | None:
    """Scan transaction instruction data for a Wormhole PostedVAA containing a Stellar pubkey.

    Wormhole VAAs encode the emitter chain (u16) and emitter address (32 bytes)
    at bytes 9-43 of the VAA payload.  Stellar chain ID on Wormhole is 6.
    The emitter address for Stellar is the Stellar account's raw 32-byte ed25519 key,
    which can be re-encoded to a G... address via base58-check.
    """
    STELLAR_CHAIN_ID = 6

    instructions = (
        tx.get("transaction", {}).get("message", {}).get("instructions", [])
    )
    account_keys: list[str] = (
        tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    )

    for ix in instructions:
        prog_idx = ix.get("programIdIndex", -1)
        if prog_idx < 0 or prog_idx >= len(account_keys):
            continue
        prog = account_keys[prog_idx]
        if prog != WORMHOLE_CORE:
            continue
        data_b64 = ix.get("data", "")
        if not data_b64:
            continue
        try:
            raw = base64.b64decode(data_b64)
        except (binascii.Error, ValueError, TypeError):
            logger.debug("Skipping instruction with undecodable base64 data")
            continue

        # The VAA starts after a 1-byte discriminator (Wormhole instruction enum)
        # and a 4-byte VAA length prefix.  Minimum usable VAA is ~100 bytes.
        if len(raw) < 50:
            continue

        # Locate the guardian signatures block to find the VAA body.
        # VAA header: version(1) guardian_set_index(4) num_signatures(1) signatures(66*n)
        offset = 1  # skip instruction discriminator
        if len(raw) < offset + 6:
            continue
        vaa_version = raw[offset]
        if vaa_version != 1:
            # Only VAA version 1 is currently defined by Wormhole; skip unknown formats.
            continue
        num_sigs = raw[offset + 5]
        body_start = offset + 6 + 66 * num_sigs

        if len(raw) < body_start + 26:
            continue

        # VAA body: timestamp(4) nonce(4) emitter_chain(2) emitter_address(32) sequence(8) ...
        emitter_chain = struct.unpack_from(">H", raw, body_start + 8)[0]
        if emitter_chain != STELLAR_CHAIN_ID:
            continue

        emitter_bytes = raw[body_start + 10: body_start + 42]
        if len(emitter_bytes) != 32:
            continue

        stellar_addr = _stellar_pubkey_to_address(emitter_bytes)
        if stellar_addr:
            return stellar_addr

    return None


def _stellar_pubkey_to_address(raw_key: bytes) -> str | None:
    """Encode a 32-byte ed25519 key as a Stellar G... account ID (base32-check)."""
    if len(raw_key) != 32:
        return None

    VERSION_BYTE_ACCOUNT = 6 << 3  # 0x30 — encodes as 'G' in base32

    payload = bytes([VERSION_BYTE_ACCOUNT]) + raw_key
    checksum = _crc16_xmodem(payload)
    checksum_bytes = struct.pack("<H", checksum)
    encoded = base64.b32encode(payload + checksum_bytes).decode("ascii").rstrip("=")

    if encoded.startswith("G"):
        return encoded
    return None


def _crc16_xmodem(data: bytes) -> int:
    """CRC-16/XModem used by Stellar's strkey encoding."""
    crc = 0x0000
    poly = 0x1021
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc
