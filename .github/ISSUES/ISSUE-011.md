---
title: "Add Stellar Account Metadata Enrichment Pipeline"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
`ingestion/account_loader.py` currently fetches basic account data from Horizon, but the wallet graph features in `detection/feature_engineering.py` require richer account metadata — including funding source, creation time, multi-sig thresholds, signer configuration, home domain, and account merge history — that is not yet ingested or stored. Building a parallel, cached account metadata enrichment pipeline will populate these fields and significantly improve the accuracy of the seven wallet-graph ML features.

## Background & Context
The README describes seven wallet-graph ML features including `account_age`, `funding_source_similarity`, and `wash_ring_membership`. Several of these depend on account metadata from Horizon's `/accounts/{account_id}` endpoint:

- `account_age`: `created_at` timestamp from the Horizon account record (derived from the earliest transaction that created the account)
- `funding_source_similarity`: whether two accounts were funded by the same parent account (checks `account.account_id` of the `create_account` operation)
- Multi-sig configuration: `thresholds` and `signers` can reveal shared signing keys across multiple wallets (a wash-trading indicator — two "independent" wallets controlled by the same signer)
- `home_domain`: a declared domain; wash traders rarely set a home domain, while legitimate market makers often do
- Account merge history: if an account was merged into another, that merge target may be another wallet in the wash ring

Currently, `account_loader.py` either has stub implementations or fetches data sequentially. The enrichment pipeline must fetch account metadata for all wallets observed in recent trade data in parallel (bounded concurrency), cache results in SQLite with a configurable TTL, and expose the enriched data to `detection/feature_engineering.py`.

## Objectives
- [ ] Implement `AccountMetadataEnricher` in `ingestion/account_loader.py` that fetches and stores full account metadata (creation time, funding source, thresholds, signers, home domain) for a batch of wallet addresses in parallel with bounded concurrency.
- [ ] Add `AccountMetadata` Pydantic model to `ingestion/data_models.py` capturing all relevant fields from the Horizon `/accounts/{id}` response.
- [ ] Implement a SQLite-backed `AccountMetadataCache` with per-record TTL (default 24h) so recently fetched accounts are not re-fetched on every pipeline run.
- [ ] Expose `funding_source_map`, `signer_overlap_pairs`, and `home_domain_set` computed properties on `AccountMetadataEnricher` that `detection/feature_engineering.py` can consume directly.

## Technical Requirements

**Horizon `/accounts/{id}` relevant fields:**
```json
{
  "id": "GABC...",
  "account_id": "GABC...",
  "sequence": "123456789",
  "subentry_count": 5,
  "thresholds": {"low_threshold": 0, "med_threshold": 0, "high_threshold": 0},
  "flags": {"auth_required": false, "auth_revocable": false, "auth_immutable": false},
  "signers": [
    {"key": "GABC...", "weight": 1, "type": "ed25519_public_key"}
  ],
  "home_domain": "example.com",
  "last_modified_ledger": 50123456
}
```

Note: account creation time is NOT in the account record — it requires fetching the account's first transaction via `GET /accounts/{id}/transactions?order=asc&limit=1`.

**`AccountMetadata` model:**
```python
class AccountSigner(BaseModel):
    key: str
    weight: int
    signer_type: str = Field(alias="type")

class AccountThresholds(BaseModel):
    low_threshold: int
    med_threshold: int
    high_threshold: int

class AccountMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    account_id: str
    sequence: str
    subentry_count: int
    thresholds: AccountThresholds
    signers: list[AccountSigner]
    home_domain: str | None = None
    last_modified_ledger: int
    created_at: datetime | None = None       # from first transaction
    funding_source: str | None = None        # account that created this account
    is_merged: bool = False                  # True if account has been merged
    merge_destination: str | None = None    # account it was merged into
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
```

**SQLite cache schema:**
```sql
CREATE TABLE IF NOT EXISTS account_metadata (
    account_id TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_metadata_expires ON account_metadata (expires_at);
```

**`AccountMetadataCache`:**
```python
class AccountMetadataCache:
    def __init__(self, db_conn: sqlite3.Connection, ttl_seconds: int = 86400): ...

    def get(self, account_id: str) -> AccountMetadata | None:
        """Return cached metadata if not expired, else None."""

    def put(self, metadata: AccountMetadata) -> None:
        """Insert or replace (upsert) into cache."""

    def get_batch(self, account_ids: list[str]) -> dict[str, AccountMetadata]:
        """Batch fetch from cache; returns only non-expired entries."""

    def prune_expired(self) -> int:
        """Delete expired entries. Returns count deleted."""
```

**`AccountMetadataEnricher.enrich_batch()` algorithm:**
```python
async def enrich_batch(
    self,
    account_ids: list[str],
    force_refresh: bool = False,
) -> dict[str, AccountMetadata]:
    # 1. Check cache for all IDs
    cached = {} if force_refresh else self.cache.get_batch(account_ids)
    missing = [aid for aid in account_ids if aid not in cached]

    # 2. Fetch missing accounts in parallel (bounded by semaphore)
    sem = asyncio.Semaphore(self.concurrency)
    tasks = [self._fetch_account(aid, sem) for aid in missing]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    # 3. For each successfully fetched account, also fetch creation transaction
    # 4. Store in cache; merge with cached results
    # 5. Return combined dict
```

**Funding source resolution**: fetch the earliest transaction for the account and look for a `create_account` operation where `account = this_account_id`. The `funder` field of that operation is the funding source. This requires a second Horizon call per account — batch these as a second async gather after the account fetch.

**Signer overlap detection** computed property:
```python
@property
def signer_overlap_pairs(self) -> list[tuple[str, str]]:
    """
    Return pairs of (account_a, account_b) where both accounts share
    at least one non-master signer key (weight > 0, key != account_id).
    O(n^2) in accounts; acceptable for typical wallet batches <= 1000.
    """
```

**Configuration** (add to `config/settings.py`):
- `ACCOUNT_ENRICHMENT_CONCURRENCY`: default `10`
- `ACCOUNT_METADATA_TTL_SECONDS`: default `86400` (24h)
- `ACCOUNT_ENRICHMENT_ENABLED`: default `True`
- `ACCOUNT_FETCH_CREATION_TIME`: default `True` (set `False` to skip the second Horizon call for creation time — faster but omits `account_age` feature)

## Security Considerations
- Account IDs must be validated as 56-character Stellar public keys (`G...`) before being used in Horizon API calls. Invalid IDs (e.g., injection strings) must raise `ValueError` and be logged, not silently skipped.
- `home_domain` values from Horizon must be treated as untrusted strings — never used in DNS lookups or HTTP requests by this module. Log them as-is for analysis only.
- The `signer_overlap_pairs` computation reveals that two wallets share control. This is sensitive intelligence — do not log wallet addresses at `INFO` level; only log counts (e.g., `"Found 3 signer-overlap pairs"` not the actual addresses).
- Fetched account metadata must not include the account's `sequence` number in logs or the API response, as this could leak information useful for transaction ordering attacks.
- Cache TTL must be enforced strictly — stale metadata (e.g., a wallet that has changed its signers since last fetch) could produce incorrect signer-overlap detections.

## Testing Requirements
- Unit tests covering `AccountMetadata` model: valid construction from Horizon JSON fixture, missing optional fields, non-native signer types
- Unit tests covering `AccountMetadataCache`: get hit, get miss (expired), get miss (absent), put/upsert, `prune_expired` count
- Unit tests covering `AccountMetadataEnricher.enrich_batch()`: all cached (no HTTP calls), all missing (all fetched), mixed case, one fetch failure (exception returned from gather)
- Unit tests covering `signer_overlap_pairs`: no overlap, one pair sharing a signer, three-way overlap
- Unit tests covering `funding_source` extraction from first transaction
- Integration tests: mock Horizon `/accounts/{id}` + `/accounts/{id}/transactions` for 5 accounts; run `enrich_batch`; assert all metadata written to cache and returned
- Edge cases: account not found (404 → `AccountMetadata` with `is_merged=True` or skip), account with no signers, account with 10+ signers, `home_domain` containing special characters, `sequence` as very large integer string
- Performance benchmark: enriching 100 accounts with `concurrency=10` against a mock server should complete in < 5 seconds

## Documentation Requirements
- Update `README.md` feature groups section to fully document what `account_age` and `funding_source_similarity` measure and what data they draw from
- Add docstrings to `AccountMetadataEnricher`, `AccountMetadataCache`, `AccountMetadata`, and `signer_overlap_pairs`
- Update `docs/ingestion.md` with a section on account enrichment, cache TTL tuning, and the `ACCOUNT_FETCH_CREATION_TIME` trade-off
- Add a note in `config/settings.py` explaining that `ACCOUNT_ENRICHMENT_CONCURRENCY` should respect Horizon's per-IP rate limit when running alongside the streamer

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Stellar account model, Horizon accounts/transactions API, Python async HTTP, SQLite caching, Pydantic v2
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with solid knowledge of the Stellar account model (signers, thresholds, home domain, account creation). Experience building parallel data enrichment pipelines with SQLite-backed caching. Familiarity with the Horizon accounts and transactions API endpoints is essential.
