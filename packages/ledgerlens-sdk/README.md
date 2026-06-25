# ledgerlens-sdk

Typed Python client for the [LedgerLens](https://github.com/Ledger-Lenz/Ledgerlens-core)
wash-trading detection API. Standalone package — depends only on `httpx`
and `pydantic`, not on the `ledgerlens-core` detection engine itself.

## Install

```bash
pip install ledgerlens-sdk
```

(Not yet published to PyPI — see "Publishing" below.)

## Usage

### Synchronous

```python
from ledgerlens import LedgerLensClient

with LedgerLensClient(base_url="https://api.ledgerlens.io", api_key="...") as client:
    result = client.get_score("GABCDEF...")
    for score in result.scores:
        print(score.asset_pair, score.score)
```

### Asynchronous

```python
import asyncio
from ledgerlens import AsyncLedgerLensClient

async def main():
    async with AsyncLedgerLensClient(base_url="https://api.ledgerlens.io") as client:
        wallets = ["GABC...", "GDEF...", "GHIJ..."]
        results = await asyncio.gather(*(client.get_score(w) for w in wallets))
        for r in results:
            print(r.scores)

asyncio.run(main())
```

### Error handling

Every non-2xx response raises `LedgerLensAPIError`:

```python
from ledgerlens import LedgerLensClient, LedgerLensAPIError

client = LedgerLensClient(base_url="https://api.ledgerlens.io")
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
