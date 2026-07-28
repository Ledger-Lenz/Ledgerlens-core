"""Tests for ``ingestion.solana_adapter``.

Design notes
------------
Every HTTP interaction is served by :class:`_RpcCassette`, an ``httpx``
transport that routes on the JSON-RPC *method* name instead of relying on
call ordering.  The old queue-based transport popped responses positionally,
so adding a single RPC call anywhere in the adapter silently shifted every
canned response by one (or raised ``IndexError`` from a background pop).

The adapter is always constructed through the ``adapter_factory`` fixture,
which pins dedup off by default and otherwise binds an **in-memory** sqlite
store.  Previously the tests instantiated ``SolanaAdapter()`` directly, which
built an ``IdempotencyKeyStore`` against the on-disk ``ledgerlens.db``: the
first run recorded the mock signature and every later run saw it as a
duplicate, so ``test_ingest_cassette`` was order- and history-dependent.
"""

from __future__ import annotations

import base64
import struct

import httpx
import pytest

from ingestion.data_models import Trade
from ingestion.solana_adapter import (
    OPENBOOK_DEX,
    SERUM_DEX_V3,
    WORMHOLE_CORE,
    SolanaAdapter,
    _crc16_xmodem,
    _extract_spl_token_changes,
    _extract_stellar_address_from_vaa,
    _is_dex_transaction,
    _rpc_url,
    _stellar_pubkey_to_address,
    _tx_to_trade,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MOCK_SIG = "5wUjhZzYiMnHNz3rZMzHxnrkW8Y7YdBzxxQRiM4tWjTqBp7KkBa9N2gZYjHkNqTzJ6bJLvFRc5mRkXcQhVbMNfU"
_MOCK_SIG_2 = "2bQeTxZzYiMnHNz3rZMzHxnrkW8Y7YdBzxxQRiM4tWjTqBp7KkBa9N2gZYjHkNqTzJ6bJLvFRc5mRkXcQhVbMabc"
_MOCK_ADDRESS = "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5ZARQ"
_COUNTERPARTY = "ACCT_B"
_TOKEN_A = "So11111111111111111111111111111111111111112"   # wSOL
_TOKEN_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC

_BLOCK_TIME = 1_700_000_000


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _balance(index: int, mint: str, owner: str, amount: float) -> dict:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"uiAmount": amount},
    }


def _make_tx(
    *,
    block_time: int | None = _BLOCK_TIME,
    dex_program: str | None = SERUM_DEX_V3,
    pre_balances: list[dict] | None = None,
    post_balances: list[dict] | None = None,
    instructions: list[dict] | None = None,
    extra_accounts: list[str] | None = None,
) -> dict:
    """Build a Solana ``getTransaction`` result.

    The default swap moves 2 wSOL from ``_MOCK_ADDRESS`` to ``_COUNTERPARTY``
    in exchange for 20 USDC, i.e. a price of 10 USDC per wSOL.
    """
    account_keys = [_MOCK_ADDRESS, _COUNTERPARTY]
    if dex_program:
        account_keys.append(dex_program)
    account_keys.extend(extra_accounts or [])

    pre = pre_balances if pre_balances is not None else [
        _balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0),
        _balance(1, _TOKEN_B, _COUNTERPARTY, 100.0),
    ]
    post = post_balances if post_balances is not None else [
        _balance(0, _TOKEN_A, _MOCK_ADDRESS, 8.0),     # sold 2 wSOL
        _balance(1, _TOKEN_B, _COUNTERPARTY, 80.0),    # sold 20 USDC
        _balance(0, _TOKEN_B, _MOCK_ADDRESS, 20.0),    # bought 20 USDC
        _balance(1, _TOKEN_A, _COUNTERPARTY, 2.0),     # bought 2 wSOL
    ]
    return {
        "blockTime": block_time,
        "transaction": {
            "message": {
                "accountKeys": account_keys,
                "instructions": instructions or [],
            }
        },
        "meta": {"preTokenBalances": pre, "postTokenBalances": post},
    }


class _RpcCassette(httpx.BaseTransport):
    """Method-routed JSON-RPC cassette.

    ``signatures`` is returned for ``getSignaturesForAddress``; ``transactions``
    maps a signature to its ``getTransaction`` result (a missing signature
    yields a ``null`` result, exactly like a pruned Solana node).  Unknown
    methods raise so a silently-added RPC call fails loudly instead of
    consuming another endpoint's canned response.
    """

    def __init__(
        self,
        signatures: list[dict] | None = None,
        transactions: dict[str, dict | None] | None = None,
        rpc_error: dict | None = None,
    ) -> None:
        self.signatures = signatures or []
        self.transactions = transactions or {}
        self.rpc_error = rpc_error
        self.requests: list[tuple[str, list]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", [])
        self.requests.append((method, params))

        if self.rpc_error is not None:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": self.rpc_error}
            )

        if method == "getSignaturesForAddress":
            result: object = self.signatures
        elif method == "getTransaction":
            result = self.transactions.get(params[0])
        else:  # pragma: no cover - guard against untested RPC calls
            raise AssertionError(f"unexpected RPC method: {method}")

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    @property
    def methods_called(self) -> list[str]:
        return [m for m, _ in self.requests]


@pytest.fixture
def adapter_factory(monkeypatch):
    """Build ``SolanaAdapter`` instances with dedup explicitly controlled.

    Dedup defaults to *off* so ingest tests never touch the repo-level sqlite
    database and never depend on what a previous run recorded.
    """
    import config.settings as settings_module

    monkeypatch.setattr(
        settings_module.settings, "ingestion_dedup_enabled", False, raising=False
    )

    def _factory(*, dedup: bool = False, **kwargs) -> SolanaAdapter:
        if dedup:
            from ingestion.dedup import IdempotencyKeyStore

            kwargs.setdefault("dedup_store", IdempotencyKeyStore(db_path=":memory:"))
        return SolanaAdapter(**kwargs)

    return _factory


@pytest.fixture
def client_factory():
    """Yield a helper returning an httpx.Client bound to a cassette."""
    created: list[httpx.Client] = []

    def _make(cassette: _RpcCassette) -> httpx.Client:
        client = httpx.Client(transport=cassette)
        created.append(client)
        return client

    yield _make

    for client in created:
        client.close()


@pytest.fixture(autouse=True)
def _isolate_rpc_env(monkeypatch):
    """Ensure no test leaks SOLANA_RPC_URL into another."""
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    monkeypatch.delenv("SOLANA_REQUEST_TIMEOUT", raising=False)


# ---------------------------------------------------------------------------
# _extract_spl_token_changes
# ---------------------------------------------------------------------------


def test_extract_spl_token_changes_returns_exact_deltas():
    changes = _extract_spl_token_changes(_make_tx())

    assert sorted(changes) == sorted(
        [
            (_MOCK_ADDRESS, _TOKEN_A, -2.0),
            (_MOCK_ADDRESS, _TOKEN_B, 20.0),
            (_COUNTERPARTY, _TOKEN_A, 2.0),
            (_COUNTERPARTY, _TOKEN_B, -20.0),
        ]
    )


def test_extract_spl_token_changes_is_deterministically_ordered():
    """Ordering must not depend on RPC response ordering."""
    tx = _make_tx()
    shuffled = _make_tx(post_balances=list(reversed(tx["meta"]["postTokenBalances"])))

    assert _extract_spl_token_changes(tx) == _extract_spl_token_changes(shuffled)


def test_extract_spl_token_changes_empty_tx():
    assert _extract_spl_token_changes({}) == []


def test_extract_spl_token_changes_ignores_unchanged_balances():
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
        post_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
    )
    assert _extract_spl_token_changes(tx) == []


def test_extract_spl_token_changes_detects_closed_token_account():
    """A drained/closed account appears only in preTokenBalances.

    Regression test: keying only off ``postTokenBalances`` lost the sell leg
    entirely, which turned a real swap into an un-mappable buy-only tx.
    """
    tx = _make_tx(
        pre_balances=[
            _balance(0, _TOKEN_A, _MOCK_ADDRESS, 5.0),
            _balance(1, _TOKEN_B, _COUNTERPARTY, 50.0),
        ],
        post_balances=[
            # account 0 was closed after the swap; only the buy leg remains
            _balance(1, _TOKEN_B, _COUNTERPARTY, 30.0),
            _balance(1, _TOKEN_A, _COUNTERPARTY, 5.0),
        ],
    )
    changes = _extract_spl_token_changes(tx)

    assert (_MOCK_ADDRESS, _TOKEN_A, -5.0) in changes
    assert (_COUNTERPARTY, _TOKEN_A, 5.0) in changes


def test_extract_spl_token_changes_tolerates_null_amounts():
    tx = _make_tx(
        pre_balances=[{"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
                       "uiTokenAmount": {"uiAmount": None}}],
        post_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 3.0)],
    )
    assert _extract_spl_token_changes(tx) == [(_MOCK_ADDRESS, _TOKEN_A, 3.0)]


def test_extract_spl_token_changes_skips_ownerless_entries():
    tx = _make_tx(
        pre_balances=[{"accountIndex": 0, "mint": _TOKEN_A,
                       "uiTokenAmount": {"uiAmount": 1.0}}],
        post_balances=[{"accountIndex": 0, "mint": _TOKEN_A,
                        "uiTokenAmount": {"uiAmount": 5.0}}],
    )
    assert _extract_spl_token_changes(tx) == []


def test_extract_spl_token_changes_ignores_dust_below_epsilon():
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 1.0)],
        post_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 1.0 + 1e-12)],
    )
    assert _extract_spl_token_changes(tx) == []


# ---------------------------------------------------------------------------
# _tx_to_trade
# ---------------------------------------------------------------------------


def test_tx_to_trade_maps_canonical_fields():
    trade = _tx_to_trade(_make_tx(), _MOCK_SIG)

    assert isinstance(trade, Trade)
    assert trade.id == _MOCK_SIG
    assert trade.transaction_hash == _MOCK_SIG
    assert trade.source == "solana"
    assert trade.base_account == _MOCK_ADDRESS
    assert trade.counter_account == _COUNTERPARTY
    assert trade.base_asset.issuer == _TOKEN_A
    assert trade.counter_asset.issuer == _TOKEN_B
    assert trade.base_amount == pytest.approx(2.0)
    assert trade.counter_amount == pytest.approx(20.0)
    assert trade.price == pytest.approx(10.0)
    assert trade.base_is_seller is True
    assert trade.ledger_close_time.timestamp() == _BLOCK_TIME
    assert trade.ledger_close_time.tzinfo is not None


def test_tx_to_trade_is_deterministic_across_balance_ordering():
    tx = _make_tx()
    shuffled = _make_tx(post_balances=list(reversed(tx["meta"]["postTokenBalances"])))

    a, b = _tx_to_trade(tx, _MOCK_SIG), _tx_to_trade(shuffled, _MOCK_SIG)
    assert a is not None and b is not None
    assert (a.base_asset.issuer, a.base_amount, a.price) == (
        b.base_asset.issuer,
        b.base_amount,
        b.price,
    )


def test_tx_to_trade_self_trade_has_no_counter_account():
    """Both legs owned by one wallet ⇒ counter_account collapses to None."""
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
        post_balances=[
            _balance(0, _TOKEN_A, _MOCK_ADDRESS, 8.0),
            _balance(1, _TOKEN_B, _MOCK_ADDRESS, 20.0),
        ],
    )
    trade = _tx_to_trade(tx, _MOCK_SIG)

    assert trade is not None
    assert trade.base_account == _MOCK_ADDRESS
    assert trade.counter_account is None


@pytest.mark.parametrize("block_time", [None, "not-a-number", float("nan")])
def test_tx_to_trade_rejects_invalid_block_time(block_time):
    """Regression: a non-integer blockTime used to raise out of the mapper."""
    assert _tx_to_trade(_make_tx(block_time=block_time), _MOCK_SIG) is None


def test_tx_to_trade_no_changes():
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
        post_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
    )
    assert _tx_to_trade(tx, _MOCK_SIG) is None


def test_tx_to_trade_one_sided_transfer_is_not_a_trade():
    """Two buys and no sell cannot be priced as a swap."""
    tx = _make_tx(
        pre_balances=[],
        post_balances=[
            _balance(0, _TOKEN_A, _MOCK_ADDRESS, 2.0),
            _balance(1, _TOKEN_B, _COUNTERPARTY, 20.0),
        ],
    )
    assert _tx_to_trade(tx, _MOCK_SIG) is None


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_tx_to_trade_rejects_non_finite_amounts(bad):
    """Regression: Inf/NaN amounts raised a pydantic ValidationError.

    ``Trade`` requires finite, strictly-positive amounts, so a malformed RPC
    payload used to blow up the mapper instead of being skipped.
    """
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, 10.0)],
        post_balances=[
            _balance(0, _TOKEN_A, _MOCK_ADDRESS, 8.0),
            _balance(1, _TOKEN_B, _COUNTERPARTY, bad),
        ],
    )
    assert _tx_to_trade(tx, _MOCK_SIG) is None


def test_extract_spl_token_changes_drops_non_finite_amounts():
    tx = _make_tx(
        pre_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, float("nan"))],
        post_balances=[_balance(0, _TOKEN_A, _MOCK_ADDRESS, float("inf"))],
    )
    assert _extract_spl_token_changes(tx) == []


def test_tx_to_trade_never_raises_on_malformed_meta():
    for tx in (
        {},
        {"blockTime": _BLOCK_TIME},
        {"blockTime": _BLOCK_TIME, "meta": None},
        {"blockTime": _BLOCK_TIME, "meta": {"preTokenBalances": None,
                                            "postTokenBalances": None}},
    ):
        assert _tx_to_trade(tx, _MOCK_SIG) is None


# ---------------------------------------------------------------------------
# _is_dex_transaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("program", [SERUM_DEX_V3, OPENBOOK_DEX])
def test_is_dex_transaction_recognises_known_programs(program):
    assert _is_dex_transaction(_make_tx(dex_program=program)) is True


def test_is_dex_transaction_no_program():
    assert _is_dex_transaction(_make_tx(dex_program=None)) is False


def test_is_dex_transaction_unknown_program():
    assert _is_dex_transaction(_make_tx(dex_program=WORMHOLE_CORE)) is False


def test_is_dex_transaction_empty_tx():
    assert _is_dex_transaction({}) is False


# ---------------------------------------------------------------------------
# SolanaAdapter.ingest (cassette-driven)
# ---------------------------------------------------------------------------


def test_ingest_maps_single_dex_trade(adapter_factory, client_factory):
    cassette = _RpcCassette(
        signatures=[{"signature": _MOCK_SIG, "err": None, "memo": None}],
        transactions={_MOCK_SIG: _make_tx()},
    )
    adapter = adapter_factory()

    trades = adapter.ingest(_MOCK_ADDRESS, limit=10, client=client_factory(cassette))

    assert len(trades) == 1
    assert trades[0].source == "solana"
    assert trades[0].transaction_hash == _MOCK_SIG
    assert _MOCK_ADDRESS in (trades[0].base_account, trades[0].counter_account)
    assert cassette.methods_called == ["getSignaturesForAddress", "getTransaction"]


def test_ingest_is_idempotent_across_repeated_calls(adapter_factory, client_factory):
    """Same cassette twice ⇒ same trades (no hidden per-run state)."""
    adapter = adapter_factory()
    payload = {
        "signatures": [{"signature": _MOCK_SIG}],
        "transactions": {_MOCK_SIG: _make_tx()},
    }

    first = adapter.ingest(_MOCK_ADDRESS, client=client_factory(_RpcCassette(**payload)))
    second = adapter.ingest(_MOCK_ADDRESS, client=client_factory(_RpcCassette(**payload)))

    assert [t.id for t in first] == [t.id for t in second] == [_MOCK_SIG]


def test_ingest_skips_non_dex_transactions(adapter_factory, client_factory):
    cassette = _RpcCassette(
        signatures=[{"signature": _MOCK_SIG}],
        transactions={_MOCK_SIG: _make_tx(dex_program=None)},
    )
    assert adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette)) == []


def test_ingest_empty_signature_list(adapter_factory, client_factory):
    cassette = _RpcCassette(signatures=[])

    assert adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette)) == []
    assert cassette.methods_called == ["getSignaturesForAddress"]


def test_ingest_skips_blank_and_missing_signatures(adapter_factory, client_factory):
    cassette = _RpcCassette(
        signatures=[{"signature": ""}, {}, {"signature": _MOCK_SIG}],
        transactions={_MOCK_SIG: _make_tx()},
    )

    trades = adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette))

    assert [t.id for t in trades] == [_MOCK_SIG]
    assert cassette.methods_called.count("getTransaction") == 1


def test_ingest_tolerates_pruned_transaction(adapter_factory, client_factory):
    """A ``null`` getTransaction result must not abort the batch."""
    cassette = _RpcCassette(
        signatures=[{"signature": _MOCK_SIG_2}, {"signature": _MOCK_SIG}],
        transactions={_MOCK_SIG_2: None, _MOCK_SIG: _make_tx()},
    )

    trades = adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette))

    assert [t.id for t in trades] == [_MOCK_SIG]


def test_ingest_propagates_signature_rpc_error(adapter_factory, client_factory):
    """Errors on the *signature* call are fatal — they are not per-tx noise."""
    cassette = _RpcCassette(rpc_error={"code": -32005, "message": "rate limited"})

    with pytest.raises(RuntimeError, match="Solana RPC error"):
        adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette))


def test_ingest_passes_before_signature_cursor(adapter_factory, client_factory):
    cassette = _RpcCassette(signatures=[])

    adapter_factory().ingest(
        _MOCK_ADDRESS, limit=7, before_signature=_MOCK_SIG,
        client=client_factory(cassette),
    )

    _, params = cassette.requests[0]
    assert params[0] == _MOCK_ADDRESS
    assert params[1] == {"limit": 7, "commitment": "finalized", "before": _MOCK_SIG}


def test_ingest_omits_before_when_not_paginating(adapter_factory, client_factory):
    """Regression: the cursor was written into a shared params dict."""
    cassette = _RpcCassette(signatures=[])

    adapter_factory().ingest(_MOCK_ADDRESS, client=client_factory(cassette))

    assert "before" not in cassette.requests[0][1][1]


def test_ingest_closes_client_it_created(adapter_factory, monkeypatch):
    created: list[httpx.Client] = []
    cassette = _RpcCassette(signatures=[])
    real_client = httpx.Client

    def _tracked(*args, **kwargs):
        client = real_client(transport=cassette)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", _tracked)
    adapter_factory().ingest(_MOCK_ADDRESS)

    assert len(created) == 1
    assert created[0].is_closed


def test_ingest_does_not_close_caller_supplied_client(adapter_factory, client_factory):
    client = client_factory(_RpcCassette(signatures=[]))

    adapter_factory().ingest(_MOCK_ADDRESS, client=client)

    assert not client.is_closed


# ---------------------------------------------------------------------------
# Dedup integration (in-memory store, no repo database touched)
# ---------------------------------------------------------------------------


def test_ingest_deduplicates_repeated_signature(adapter_factory, client_factory):
    import time

    from ingestion.dedup import IdempotencyKeyStore

    store = IdempotencyKeyStore(db_path=":memory:")
    adapter = adapter_factory(dedup_store=store)
    tx = _make_tx(block_time=int(time.time()))

    def _run():
        cassette = _RpcCassette(
            signatures=[{"signature": _MOCK_SIG}], transactions={_MOCK_SIG: tx}
        )
        return adapter.ingest(_MOCK_ADDRESS, client=client_factory(cassette))

    assert len(_run()) == 1
    assert _run() == []


def test_ingest_without_dedup_store_does_not_touch_database(adapter_factory):
    assert adapter_factory().dedup_store is None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_rpc_url_defaults_to_mainnet():
    assert _rpc_url() == "https://api.mainnet-beta.solana.com"


def test_rpc_url_from_env(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
    assert _rpc_url() == "https://api.devnet.solana.com"


def test_constructor_rpc_url_does_not_leak_into_environment(adapter_factory):
    """Regression: ``__init__`` used ``os.environ.setdefault``.

    That mutated global process state for every other adapter and — worse —
    silently ignored the argument whenever SOLANA_RPC_URL was already set.
    """
    import os

    adapter = adapter_factory(rpc_url="https://private.rpc.example")

    assert "SOLANA_RPC_URL" not in os.environ
    assert adapter._effective_rpc_url() == "https://private.rpc.example"


def test_constructor_rpc_url_overrides_environment(adapter_factory, monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")

    adapter = adapter_factory(rpc_url="https://private.rpc.example")

    assert adapter._effective_rpc_url() == "https://private.rpc.example"


def test_ingest_posts_to_constructor_rpc_url(adapter_factory, client_factory):
    cassette = _RpcCassette(signatures=[])
    client = client_factory(cassette)
    captured: list[str] = []
    original_post = client.post

    def _spy(url, **kwargs):
        captured.append(url)
        return original_post(url, **kwargs)

    client.post = _spy  # type: ignore[method-assign]
    adapter_factory(rpc_url="https://private.rpc.example").ingest(
        _MOCK_ADDRESS, client=client
    )

    assert captured == ["https://private.rpc.example"]


# ---------------------------------------------------------------------------
# Stellar strkey helpers
# ---------------------------------------------------------------------------


def test_crc16_xmodem_known_vector():
    """The canonical CRC-16/XMODEM check value for b"123456789" is 0x31C3."""
    assert _crc16_xmodem(b"123456789") == 0x31C3


def test_crc16_xmodem_empty_input():
    assert _crc16_xmodem(b"") == 0x0000


def test_crc16_xmodem_stays_in_range():
    for payload in (b"\x00", b"\xff" * 64, bytes(range(256))):
        assert 0 <= _crc16_xmodem(payload) <= 0xFFFF


def test_stellar_pubkey_to_address_known_vector():
    raw = bytes.fromhex(
        "3f0c34bf93ad0d9971d04ccc90f705511c838aad9734a4a2fb0d7a03fc7fe89a"
    )
    assert (
        _stellar_pubkey_to_address(raw)
        == "GA7QYNF7SOWQ3GLR2BGMZEHXAVIRZA4KVWLTJJFC7MGXUA74P7UJVSGZ"
    )


def test_stellar_pubkey_to_address_shape():
    addr = _stellar_pubkey_to_address(bytes(range(32)))

    assert isinstance(addr, str)
    assert addr.startswith("G")
    assert len(addr) == 56
    assert "=" not in addr


@pytest.mark.parametrize("length", [0, 1, 10, 31, 33, 64])
def test_stellar_pubkey_to_address_rejects_wrong_length(length):
    assert _stellar_pubkey_to_address(b"\x00" * length) is None


# ---------------------------------------------------------------------------
# Wormhole VAA parsing
# ---------------------------------------------------------------------------


def _vaa_instruction_data(
    emitter_chain: int,
    emitter_address: bytes,
    num_signatures: int = 1,
) -> str:
    """Build base64 Wormhole instruction data wrapping a signed VAA.

    Layout: discriminator(1) | version(1) guardian_set(4) num_sigs(1)
            | signatures(66*n) | body{ timestamp(4) nonce(4) chain(2) emitter(32) ... }
    """
    header = (
        b"\x02"                                  # instruction discriminator
        + b"\x01"                                # VAA version
        + struct.pack(">I", 0)                   # guardian set index
        + bytes([num_signatures])
    )
    signatures = b"\x00" * (66 * num_signatures)
    body = (
        struct.pack(">I", _BLOCK_TIME)           # timestamp
        + struct.pack(">I", 42)                  # nonce
        + struct.pack(">H", emitter_chain)       # emitter chain
        + emitter_address                        # emitter address (32 bytes)
        + struct.pack(">Q", 7)                   # sequence
        + b"\x01"                                # consistency level
    )
    return base64.b64encode(header + signatures + body).decode("ascii")


def _wormhole_tx(data_b64: str, program: str = WORMHOLE_CORE) -> dict:
    return {
        "transaction": {
            "message": {
                "accountKeys": [program],
                "instructions": [{"programIdIndex": 0, "data": data_b64}],
            }
        }
    }


def test_extract_stellar_address_from_vaa_happy_path():
    raw_key = bytes.fromhex(
        "3f0c34bf93ad0d9971d04ccc90f705511c838aad9734a4a2fb0d7a03fc7fe89a"
    )
    tx = _wormhole_tx(_vaa_instruction_data(emitter_chain=6, emitter_address=raw_key))

    assert _extract_stellar_address_from_vaa(tx) == _stellar_pubkey_to_address(raw_key)


@pytest.mark.parametrize("num_signatures", [0, 1, 3, 13])
def test_extract_stellar_address_from_vaa_varying_guardian_signatures(num_signatures):
    """Body offset must track the variable-length guardian signature block."""
    raw_key = bytes(range(32))
    tx = _wormhole_tx(
        _vaa_instruction_data(6, raw_key, num_signatures=num_signatures)
    )

    assert _extract_stellar_address_from_vaa(tx) == _stellar_pubkey_to_address(raw_key)


def test_extract_stellar_address_from_vaa_ignores_other_chains():
    tx = _wormhole_tx(_vaa_instruction_data(emitter_chain=1, emitter_address=bytes(32)))
    assert _extract_stellar_address_from_vaa(tx) is None


def test_extract_stellar_address_from_vaa_ignores_non_wormhole_program():
    data = _vaa_instruction_data(6, bytes(range(32)))
    assert _extract_stellar_address_from_vaa(_wormhole_tx(data, SERUM_DEX_V3)) is None


def test_extract_stellar_address_from_vaa_empty_tx():
    assert _extract_stellar_address_from_vaa({}) is None


def test_extract_stellar_address_from_vaa_no_wormhole():
    assert _extract_stellar_address_from_vaa(_make_tx()) is None


@pytest.mark.parametrize(
    "instruction",
    [
        {"programIdIndex": -1, "data": "AAAA"},
        {"programIdIndex": 99, "data": "AAAA"},
        {"programIdIndex": 0, "data": ""},
        {"programIdIndex": 0, "data": "!!!not-base64!!!"},
        {"programIdIndex": 0, "data": base64.b64encode(b"\x02" * 20).decode()},
        {"programIdIndex": 0},
    ],
)
def test_extract_stellar_address_from_vaa_malformed_inputs(instruction):
    """Malformed instructions are skipped, never raised on."""
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [WORMHOLE_CORE],
                "instructions": [instruction],
            }
        }
    }
    assert _extract_stellar_address_from_vaa(tx) is None


def test_solana_adapter_rpc_url_from_env(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
    from ingestion.solana_adapter import _rpc_url
    assert _rpc_url() == "https://api.devnet.solana.com"


# ---------------------------------------------------------------------------
# __all__ exports
# ---------------------------------------------------------------------------


def test_module_all_defined():
    import ingestion.solana_adapter as m

    assert hasattr(m, "__all__")
    for name in m.__all__:
        assert hasattr(m, name), f"__all__ lists {name!r} but not defined"


# ---------------------------------------------------------------------------
# _extract_spl_token_changes — no dead account_keys variable
# ---------------------------------------------------------------------------


def test_extract_spl_token_changes_no_dead_variable():
    """Ensure _extract_spl_token_changes does not reference an unused accountKeys
    variable; previously the expression was evaluated and discarded, which
    cluttered the code. This test verifies the function works correctly without
    account_keys being present in the message at all."""
    tx = {
        "meta": {
            "preTokenBalances": [
                {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
                 "uiTokenAmount": {"uiAmount": 5.0}},
            ],
            "postTokenBalances": [
                {"accountIndex": 0, "mint": _TOKEN_A, "owner": _MOCK_ADDRESS,
                 "uiTokenAmount": {"uiAmount": 3.0}},
            ],
        },
        # Deliberately omit "transaction.message.accountKeys"
        "transaction": {"message": {}},
    }
    changes = _extract_spl_token_changes(tx)
    # Should still extract the -2.0 balance change for TOKEN_A.
    assert len(changes) == 1
    owner, mint, delta = changes[0]
    assert owner == _MOCK_ADDRESS
    assert mint == _TOKEN_A
    assert abs(delta - (-2.0)) < 1e-9


# ---------------------------------------------------------------------------
# VAA version check (previously the version byte was a dead expression)
# ---------------------------------------------------------------------------


def test_extract_stellar_address_from_vaa_unknown_version():
    """VAA data whose version byte != 1 must be silently skipped."""
    import base64
    import struct

    # Build a minimal fake VAA with version byte = 2 (unknown).
    WORMHOLE_CORE_PROG = "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth"

    # instruction discriminator (1 byte) + version (1) + guardian_set_index (4) +
    # num_sigs (1) + body placeholder
    num_sigs = 0
    version = 2  # unknown — should be rejected
    raw = bytes([0x00, version, 0, 0, 0, 0, num_sigs]) + bytes(200)
    data_b64 = base64.b64encode(raw).decode()

    tx = {
        "transaction": {
            "message": {
                "accountKeys": [WORMHOLE_CORE_PROG],
                "instructions": [
                    {"programIdIndex": 0, "data": data_b64},
                ],
            }
        },
        "meta": {},
    }
    # Should return None because version != 1.
    assert _extract_stellar_address_from_vaa(tx) is None
