# ledgerlens-sdk

Typed Python client for the [LedgerLens](https://github.com/Ledger-Lenz/Ledgerlens-core)
wash-trading detection API. Standalone package — depends only on `httpx`
and `pydantic`, not on the `ledgerlens-core` detection engine itself.

See [CHANGELOG.md](CHANGELOG.md) for a full list of notable changes between
releases.

## Install

```bash
pip install ledgerlens-sdk
```

(Not yet published to PyPI — see "Publishing" below.)

## Quick Start

The snippet below is self-contained and runnable. It shows the full
install-to-first-call flow against either the hosted API or a local
`ledgerlens-core` dev server (`python cli.py serve`).

```python
from ledgerlens import LedgerLensClient, LedgerLensAPIError

# Point at the hosted API, or your local dev server:
#   base_url="http://localhost:8000"  (after `python cli.py serve`)
BASE_URL = "https://api.ledgerlens.io"
API_KEY  = "your-api-key"           # omit or pass None for public endpoints

with LedgerLensClient(base_url=BASE_URL, api_key=API_KEY) as client:

    # 1. Check that the API and its backing services are healthy.
    health = client.health()
    print(f"API status: {health.status}  db={health.db}  models={health.models}")
    # → API status: ok  db=ok  models=ok

    # 2. Fetch all risk scores for a specific Stellar wallet.
    WALLET = "GABCDEFGHIJKLMNOPQRSTUVWXYZ012345678901234567890123456789"
    try:
        response = client.get_score(WALLET)
    except LedgerLensAPIError as exc:
        print(f"[{exc.status_code}] {exc.detail}")
    else:
        for score in response.scores:
            print(
                f"wallet={score.wallet[:8]}…  "
                f"pair={score.asset_pair}  "
                f"score={score.score}/100  "
                f"benford={score.benford_flag}  "
                f"ml={score.ml_flag}  "
                f"confidence={score.confidence}"
            )
        # → wallet=GABCDEFG…  pair=XLM/USDC  score=82/100  benford=True  ml=True  confidence=90

        # Cross-chain links (Ethereum/Base/Polygon bridge activity), if any:
        for link in response.cross_chain_links:
            print(f"  linked {link.chain} wallet: {link.evm_wallet}")

    # 3. Retrieve the highest-risk wallets across all asset pairs.
    top_risky = client.list_scores(min_score=70, limit=10, sort_by="score")
    for s in top_risky:
        print(f"{s.wallet[:8]}… | {s.asset_pair} | score={s.score}")

    # 4. See which asset pairs carry the most aggregate risk.
    for ranking in client.asset_risk_ranking():
        print(f"{ranking.asset_pair}: avg_score={ranking.average_score:.1f}  wallets={ranking.wallet_count}")
```

**Running against your local dev server** — spin up `python cli.py serve`
from the `ledgerlens-core` root first, then substitute:

```python
BASE_URL = "http://localhost:8000"
API_KEY  = None   # or your LEDGERLENS_ADMIN_API_KEY if set
```

## Usage

### Synchronous

```python
from ledgerlens import LedgerLensClient

with LedgerLensClient(base_url="https://api.ledgerlens.io", api_key="...") as client:
    response = client.get_score("GABCDEF...")
    for score in response.scores:
        print(score.asset_pair, score.score)
```

### Asynchronous

Use `AsyncLedgerLensClient` and `async with` / `await`. Pass multiple
wallets to `asyncio.gather` to score them concurrently in a single event
loop turn:

```python
import asyncio
from ledgerlens import AsyncLedgerLensClient

async def main():
    wallets = ["GABC...", "GDEF...", "GHIJ..."]
    async with AsyncLedgerLensClient(base_url="https://api.ledgerlens.io") as client:
        results = await asyncio.gather(*(client.get_score(w) for w in wallets))
        for wallet, result in zip(wallets, results):
            for score in result.scores:
                print(f"{wallet[:8]}…  {score.asset_pair}  {score.score}/100")

asyncio.run(main())
```

### Error handling

Every non-2xx response raises `LedgerLensAPIError`. Always use the client
as a context manager (or call `client.close()` / `await client.aclose()`
explicitly) so the underlying HTTP connection pool is cleaned up:

```python
from ledgerlens import LedgerLensClient, LedgerLensAPIError

with LedgerLensClient(base_url="https://api.ledgerlens.io") as client:
    try:
        client.get_score("not-a-real-wallet")
    except LedgerLensAPIError as exc:
        print(exc.status_code, exc.detail)
```

## Endpoint coverage

Covers the primary read surface plus the most common write operations:

- `health()`
- `list_scores(...)`, `get_score(wallet)`, `explain_score(wallet, asset_pair)`,
  `get_counterfactual(wallet, asset_pair, ...)`
- `list_alerts(...)`, `asset_risk_ranking()`, `list_rings()`, `list_correlations()`,
  `pool_risk(pool_id)`, `circular_path_payments(...)`
- `create_webhook(...)`, `list_webhooks()`, `delete_webhook(subscriber_id)`
- `create_dispute(...)`, `get_dispute(dispute_id)`
- `submit_feedback(...)` (admin-key gated)

Not yet covered (intentionally out of scope for v1 — admin/governance/model
internals, not part of the typical exchange-risk-system integration
surface): `/admin/*`, `/governance/*`, `/api/v1/model/*`,
`/wallets/{wallet}/cross-chain`, `/webhooks/dead-letters`,
`/disputes/{id}/vote`, `/compliance/*`. These follow the same `_get`/`_post`
pattern in `client.py`/`async_client.py` and can be added the same way.

## Authentication

Pass `api_key=...` to either client constructor; it's sent as the
`X-LedgerLens-Admin-Key` header on every request (harmless on public
endpoints — only admin-gated ones like `submit_feedback` check it).

## Development

```bash
pip install -e ".[test]"
pytest
```

## Publishing

This package is structured to be published to PyPI as `ledgerlens-sdk`
(`python -m build && twine upload dist/*`), with its version kept in sync
with the LedgerLens API version. Publishing itself (PyPI credentials, the
actual `twine upload`) is a release action for a maintainer to run, not
something done as part of writing the SDK.
