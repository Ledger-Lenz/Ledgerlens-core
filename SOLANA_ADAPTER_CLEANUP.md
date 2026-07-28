# Cleanup: `tests/test_solana_adapter.py` (+ `ingestion/solana_adapter.py`)

## Summary

The Solana adapter test suite could not pass in a clean checkout: 3 of its 16 tests
failed deterministically, and two more only passed by accident. Auditing the failures
showed the root cause was **not** in the tests but in the adapter's untestable seams —
so the cleanup covers both files.

Result: **16 tests (3 failing) → 73 tests (all passing)**, order-independent,
repeatable, with zero filesystem side effects.

---

## Findings

### A. Test-side defects

| # | Issue | Impact |
|---|---|---|
| A1 | `patch("httpx.Client", lambda: httpx.Client(transport=...))` — the stub took no kwargs, but `httpx.Client()` recursed into the patched name | **3 hard failures** (`TypeError: <lambda>() got an unexpected keyword argument 'transport'`) |
| A2 | `_MockTransport` popped responses **positionally** from a queue | Any added RPC call shifts every response by one; an extra call raises `IndexError` from inside the adapter's blanket `except` and vanishes |
| A3 | `SolanaAdapter()` built a real `IdempotencyKeyStore` against the **repo's `ledgerlens.db`** | `test_ingest_cassette` recorded `_MOCK_SIG` on first run; on any later run it was a duplicate and returned `[]`. Test passed once, then failed forever, and dirtied a tracked file |
| A4 | Assertions were tautological — `assert x in owners or y in owners`, `assert addr is None or addr.startswith("G")`, `assert 0 <= crc <= 0xFFFF` | Passed even if the code returned `None` or was totally wrong. `_crc16_xmodem` was never checked against a known value |
| A5 | No test ever built a **valid** VAA, so `_extract_stellar_address_from_vaa` had zero happy-path coverage (only "returns None" cases) | The entire Wormhole linking feature was effectively untested |
| A6 | `test_solana_adapter_rpc_url_from_env` set `SOLANA_RPC_URL` via monkeypatch, but the adapter's `os.environ.setdefault` could leak it | Cross-test env pollution |
| A7 | Imports inside test bodies, unused `_rpc_ok` shapes, `include_serum` bool instead of a program parameter | Friction, no OpenBook coverage |

### B. Adapter defects surfaced by the audit

| # | Issue | Severity |
|---|-------|----------|
| B1 | **Mispaired trade legs.** `_tx_to_trade` took `buys[0]` regardless of mint. Measured over all 24 orderings of one ordinary 2-party swap: **12/24 produced a `base_asset == counter_asset` trade**, the price took **3 different values (0.1 / 1.0 / 10.0)** for the *same* swap, and the counterparty was lost (`None`) in 12/24. Which result you got depended purely on RPC list ordering | **Correctness — silently corrupts ingested data** |
| B2 | **Closed token accounts dropped.** Changes were keyed off `postTokenBalances` only; an account drained and closed by the swap has no post entry, so its sell leg disappeared and the tx became an un-mappable buy-only transfer | **Correctness — drops real trades** |
| B3 | **Non-deterministic output order.** `_extract_spl_token_changes` returned changes in raw RPC order, making `sells[0]`/`buys[0]` — and therefore the trade — dependent on node response ordering | Correctness / non-reproducibility |
| B4 | **Global state mutation.** `__init__` did `os.environ.setdefault("SOLANA_RPC_URL", rpc_url)`, which (a) leaked into every other adapter in the process and (b) **silently ignored the argument** whenever the env var was already set | Hidden mutable state |
| B5 | **Shared-dict aliasing.** `_get_signatures` mutated `params[1]["before"]` on a dict it had just built inline — harmless today, a classic latent aliasing trap | Fragility |
| B6 | **Unmockable transport.** `ingest` hard-created `httpx.Client()` internally, forcing tests to patch the global `httpx.Client` (the cause of A1) | Testability |
| B7 | `ValidationError` escape. Inf/NaN/negative amounts and non-integer `blockTime` propagated a pydantic `ValidationError` out of the mapper instead of returning `None` | Robustness |
| B8 | Dead statements: a discarded `accountKeys` expression and a discarded `raw[offset]` read | Confusing no-ops |
| B9 | Off-by-one guard: needed `raw[offset+5]` but only checked `len(raw) < offset + 5` | Latent `IndexError` |
| B10 | `if "error" in data` treated an explicit `"error": null` (sent by some RPC providers) as a failure | False errors |
| B11 | ~40-line deeply nested dedup block inline in the `ingest` loop | Maintainability |

---

## Fixes

### `ingestion/solana_adapter.py`

- **B1** — Counter leg is now selected as a buy of a *different mint*, preferring the base
  owner's own incoming leg; the counterparty is resolved as the seller of that mint.
  A same-mint-only transaction correctly yields `None`.
  Re-measured over the same 24 orderings: **0/24 same-asset trades, exactly 1 distinct
  result** (`wSOL→USDC @ 10.0, ALICE→BOB`) instead of 3 prices and a lost counterparty.
- **B2** — Deltas computed over the **union** of pre/post keys, so drained/closed accounts
  still register a negative delta. Owner falls back to the pre-state entry.
- **B3** — Changes returned in sorted `(accountIndex, mint)` order → fully deterministic.
- **B4** — `rpc_url` stored on the instance (`self.rpc_url`) and resolved by
  `_effective_rpc_url()`; the constructor argument now **wins** over the environment and
  never writes to `os.environ`. All RPC helpers accept an explicit `rpc_url`.
- **B5** — `_get_signatures` builds a local `options` dict; no post-hoc mutation.
- **B6** — `ingest(..., client=None)` accepts an injected `httpx.Client`, creating and
  closing its own only when none is supplied (matching `resolve_stellar_link`).
- **B7** — `_tx_to_trade` documented and hardened to *never raise*: guards `blockTime`
  (type, range, `bool`), non-finite amounts (`math.isfinite`), and non-positive
  amounts/price.
- **B8/B9** — Dead statements removed, bound check corrected to `offset + 6`,
  blanket `except Exception` around base64 narrowed to `binascii.Error/ValueError/TypeError`
  with a debug log.
- **B10** — `if data.get("error") is not None`.
- **B11** — Dedup logic extracted to `SolanaAdapter._accept_trade()`; the `ingest` loop is
  now four lines.

Public behaviour is unchanged apart from the deliberate correctness fixes (B1, B2, B4, B10)
and one **additive**, default-`None` `client` parameter.

### `tests/test_solana_adapter.py` — rewritten

- **`_RpcCassette`** replaces the positional queue: routes on the JSON-RPC **method name**,
  maps signature → transaction, supports `null` results (pruned nodes) and injected RPC
  errors, records every request for assertion, and **raises on an unexpected method** so a
  silently-added RPC call fails loudly.
- **`adapter_factory` fixture** pins `ingestion_dedup_enabled=False` and, when dedup is
  wanted, binds an **in-memory** sqlite store. The repo database is never touched
  (verified by md5 across runs).
- **`_isolate_rpc_env` autouse fixture** clears `SOLANA_RPC_URL` / `SOLANA_REQUEST_TIMEOUT`.
- Tautologies replaced with exact assertions: full canonical field mapping
  (amounts `2.0`/`20.0`, price `10.0`, both accounts, both mints), the CRC-16/XMODEM check
  value `0x31C3` for `b"123456789"`, and a known 56-char `G…` strkey vector.
- New regression tests for every finding above, including parametrised guardian-signature
  counts (0/1/3/13), truncated-VAA sweeps, malformed-instruction cases, closed token
  accounts, deterministic ordering, client lifecycle, and the constructor-vs-env precedence.

---

## Validation

```
pytest tests/test_solana_adapter.py     →  73 passed        (was 13 passed / 3 failed)
5× repeat + pytest-randomly random order →  73 passed each time
ruff check ingestion/solana_adapter.py tests/test_solana_adapter.py → All checks passed
                                            (was 4 errors, incl. one pre-existing)
md5sum ledgerlens.db before/after        →  unchanged
20,000-iteration fuzz-harness equivalent →  no exceptions
10,976 malformed-input combos to _tx_to_trade → no exceptions
```

`tests/test_ingestion_dedup.py::test_solana_adapter_restart_dedup` (the other consumer of
this adapter) still passes. Three failures in `test_ingestion_dedup.py`
(`test_concurrent_historical_loader_dedup`, `test_horizon_streamer_checkpoint_replay_dedup`,
`test_dedup_audit_cli`) are **pre-existing on `main`** — verified identical via `git stash` —
and are outside this path.
