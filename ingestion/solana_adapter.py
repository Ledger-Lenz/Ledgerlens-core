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
import logging
import os
import struct
from datetime import datetime, timezone
from typing import Any

import httpx

from ingestion.data_models import Asset, Trade, TradeType

logger = logging.getLogger("ledgerlens.solana_adapter")

_DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

# Serum/OpenBook DEX v3 program ID on mainnet-beta
SERUM_DEX_V3 = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OPENBOOK_DEX = "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"

# Wormhole core bridge program on mainnet-beta
WORMHOLE_CORE = "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth"

_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

SOURCE_LABEL = "solana"


def _rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", _DEFAULT_RPC)


def _timeout() -> float:
    return float(os.environ.get("SOLANA_REQUEST_TIMEOUT", "30.0"))


def _post(method: str, params: list[Any], client: httpx.Client) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = client.post(_rpc_url(), json=payload, timeout=_timeout())
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Solana RPC error: {data['error']}")
    return data.get("result")


def _get_signatures(
    address: str,
    client: httpx.Client,
    limit: int = 100,
    before: str | None = None,
) -> list[dict]:
    params: list[Any] = [address, {"limit": limit, "commitment": "finalized"}]
    if before:
        params[1]["before"] = before
    result = _post("getSignaturesForAddress", params, client)
    return result or []


def _get_transaction(sig: str, client: httpx.Client) -> dict | None:
    result = _post(
        "getTransaction",
        [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}],
        client,
    )
    return result


def _is_dex_transaction(tx: dict) -> bool:
    account_keys: list[str] = (
        tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    )
    return SERUM_DEX_V3 in account_keys or OPENBOOK_DEX in account_keys


def _extract_spl_token_changes(
    tx: dict,
) -> list[tuple[str, str, float]]:
    """Return (owner_pubkey, mint, amount_change) for each SPL token balance change."""
    pre: list[dict] = tx.get("meta", {}).get("preTokenBalances", []) or []
    post: list[dict] = tx.get("meta", {}).get("postTokenBalances", []) or []

    (
        tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    )

    pre_map: dict[tuple[int, str], float] = {}
    for b in pre:
        idx = b.get("accountIndex", -1)
        mint = b.get("mint", "")
        amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
        pre_map[(idx, mint)] = amt

    changes: list[tuple[str, str, float]] = []
    for b in post:
        idx = b.get("accountIndex", -1)
        mint = b.get("mint", "")
        owner = b.get("owner", "")
        amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
        pre_amt = pre_map.get((idx, mint), 0.0)
        delta = amt - pre_amt
        if abs(delta) > 1e-9 and owner:
            changes.append((owner, mint, delta))

    return changes


def _tx_to_trade(tx: dict, sig: str) -> Trade | None:
    block_time = tx.get("blockTime")
    if block_time is None:
        return None

    ts = datetime.fromtimestamp(block_time, tz=timezone.utc)
    changes = _extract_spl_token_changes(tx)

    if len(changes) < 2:
        return None

    sells = [(owner, mint, -delta) for owner, mint, delta in changes if delta < 0]
    buys = [(owner, mint, delta) for owner, mint, delta in changes if delta > 0]
    if not sells or not buys:
        return None

    base_owner, base_mint, base_amount = sells[0]
    counter_owner, counter_mint, counter_amount = buys[0]

    price = counter_amount / base_amount if base_amount else 0.0

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
    """

    def __init__(self, rpc_url: str | None = None) -> None:
        if rpc_url:
            os.environ.setdefault("SOLANA_RPC_URL", rpc_url)

    def ingest(
        self,
        address: str,
        limit: int = 100,
        before_signature: str | None = None,
    ) -> list[Trade]:
        """Fetch SPL swap events for ``address`` and return canonical Trade records."""
        trades: list[Trade] = []
        with httpx.Client() as client:
            sigs = _get_signatures(address, client, limit=limit, before=before_signature)
            for sig_info in sigs:
                sig = sig_info.get("signature", "")
                if not sig:
                    continue
                try:
                    tx = _get_transaction(sig, client)
                    if not tx or not _is_dex_transaction(tx):
                        continue
                    trade = _tx_to_trade(tx, sig)
                    if trade:
                        trades.append(trade)
                except Exception:
                    logger.warning("Failed to process Solana tx %s", sig, exc_info=True)
        logger.info(
            "solana.ingest address=%s trades=%d", address, len(trades)
        )
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
        sigs = _get_signatures(solana_address, client, limit=50)
        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            if not sig:
                continue
            try:
                tx = _get_transaction(sig, client)
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
        except Exception:
            continue

        # The VAA starts after a 1-byte discriminator (Wormhole instruction enum)
        # and a 4-byte VAA length prefix.  Minimum usable VAA is ~100 bytes.
        if len(raw) < 50:
            continue

        # Locate the guardian signatures block to find the VAA body.
        # VAA header: version(1) guardian_set_index(4) num_signatures(1) signatures(66*n)
        offset = 1  # skip instruction discriminator
        if len(raw) < offset + 5:
            continue
        raw[offset]
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
