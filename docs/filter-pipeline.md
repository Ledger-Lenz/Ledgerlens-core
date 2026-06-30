# Trade Filter Pipeline

The filter pipeline sits at the boundary between the ingestion layer (Layer 1)
and the detection engine (Layer 2). Every trade ingested from Horizon passes
through an ordered sequence of configurable filters before it reaches feature
engineering. Trades that fail any filter are rejected and stored for review;
they never reach the ML models.

## Why filters?

The Stellar DEX processes thousands of unique asset pairs. Many of those trades
are noise that degrades detection quality:

| Category | Effect without filtering |
|---|---|
| Dust / spam assets | Sub-cent trades skew Benford's Law chi-square towards false positives |
| Test pairs (`TEST/XLM`, `DEMO/USDC`) | Inflate feature vectors with irrelevant data |
| Verified market makers | High-frequency, algorithmically-clean accounts look anomalous on Benford but are known good |
| Unsupported asset types | `credit_alphanum12` assets outside detection scope add CPU load |

## Architecture

```
Horizon API
    │
    ▼
TradeFilterPipeline.apply(trade)
    │
    ├─ AssetPairWhitelistFilter  (if whitelist non-empty: block all not listed)
    ├─ AssetPairBlacklistFilter  (block listed pairs)
    ├─ MinimumVolumeFilter       (block dust trades below threshold)
    ├─ AssetTypeFilter           (block disallowed asset types)
    └─ AccountExclusionFilter    (block excluded accounts)
         │
         ▼
  passed=True → detection engine
  passed=False → filtered_trades table (SQLite) for post-hoc review
```

Filters are applied **in order**. A trade must pass **all enabled filters**
(logical AND). The pipeline short-circuits on the first rejection — subsequent
filters are not evaluated.

## Configuration

Edit `config/filter_config.yaml`. An example with all filter types is in
`config/filter_config.yaml.example`.

### Schema

```yaml
version: "1.0"    # required; only "1.0" is currently supported

filters:
  - type: <filter_type>
    enabled: true | false
    # ... filter-specific fields
```

Disabled filters (`enabled: false`) are parsed but skipped. This lets you
keep a filter configuration ready without activating it.

---

### Filter types

#### `asset_pair_whitelist`

Allow only trades whose asset pair is in the list. An **empty `pairs` list**
means *allow all* (pass-through).

```yaml
- type: asset_pair_whitelist
  enabled: true
  pairs:
    - "XLM/USDC"
    - "XLM/BTC"
    - "AQUA/XLM"
```

Pair format: `"BASE_CODE/COUNTER_CODE"` using the asset code only (not the
issuer). Native XLM is always `"XLM"`.

---

#### `asset_pair_blacklist`

Reject trades on the listed pairs. An **empty `pairs` list** means *block
nothing*.

```yaml
- type: asset_pair_blacklist
  enabled: true
  pairs:
    - "TEST/XLM"
    - "DEMO/USDC"
    - "SPAM/XLM"
```

**Ordering note**: if you have both a whitelist and a blacklist, place the
whitelist first. A trade that passes the whitelist can still be rejected by the
blacklist if both list the same pair.

---

#### `minimum_volume`

Reject trades whose volume is below the threshold.

```yaml
- type: minimum_volume
  enabled: true
  min_volume: "0.01"      # exact Decimal — use a string to preserve precision
  volume_field: base_amount   # base_amount | counter_amount | price
```

`min_volume: "0"` disables the filter (allow all). The value is stored and
compared as a Python `Decimal` to avoid floating-point rounding errors.

---

#### `asset_type`

Restrict processing to specific Stellar asset types.

```yaml
- type: asset_type
  enabled: true
  allowed_types:
    - native          # XLM
    - credit_alphanum4    # 1–4 character codes (USDC, BTC, AQUA, …)
    - credit_alphanum12   # 5–12 character codes (longer token names)
```

Removing `credit_alphanum12` from the list excludes all long-code assets.
Removing both credit types leaves only native XLM trades.

---

#### `account_exclusion`

Reject trades where either `base_account` or `counter_account` is in the
exclusion set. Use for verified clean accounts (institutional market makers,
Stellar Foundation accounts, DEX aggregator bots).

```yaml
- type: account_exclusion
  enabled: true
  excluded_accounts:
    - "GCEZWKCA5VLDNRLN3RPRJMRZOX3Z6G5CHCGCHBZVM1PBWQ5FIWM77V4"  # Stellar Foundation
```

All keys must be valid Stellar G… public keys (56 characters). Invalid keys
raise a `ValueError` at config load time to catch typos before the pipeline
starts.

> **Security**: the exclusion list reveals which accounts have been manually
> vetted. Set file permissions to `0o640` and restrict read access to the
> service user.

---

## Hot-reload

`FilterConfigLoader` polls the config file's modification time (`mtime`) every
`FILTER_CONFIG_RELOAD_INTERVAL_SECONDS` seconds (default: 60). When a change
is detected:

1. The new YAML is parsed and validated against the Pydantic schema.
2. If validation succeeds, the filter list is **atomically swapped** under a
   threading lock. In-flight `apply()` calls finish against the old filters
   before the swap completes.
3. If validation fails, the **previous valid config is retained** and an
   `ERROR` is logged. The pipeline is never left without filters.

You can change filter rules without restarting the ingestion process.

### Configuration

| Env var | Default | Description |
|---|---|---|
| `FILTER_CONFIG_PATH` | `./config/filter_config.yaml` | Path to the config file |
| `FILTER_CONFIG_RELOAD_INTERVAL_SECONDS` | `60` | Polling interval in seconds |
| `FILTER_STORE_REJECTED_TRADES` | `true` | Persist rejected trades to SQLite |
| `FILTER_REJECTED_TRADES_MAX_ROWS` | `500000` | Prune threshold (rows) |

---

## Rejected trade storage

When `FILTER_STORE_REJECTED_TRADES=true` (the default), rejected trades are
written to the `filtered_trades` SQLite table:

```sql
CREATE TABLE IF NOT EXISTS filtered_trades (
    id TEXT NOT NULL,
    paging_token TEXT NOT NULL,
    ledger_close_time TIMESTAMP NOT NULL,
    rejection_reason TEXT NOT NULL,
    filtered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (paging_token)
);
```

Only the primary key fields and rejection reason are stored — not the full
trade record — to keep the table small. Duplicate `paging_token` values are
silently ignored.

### Pruning

When the table exceeds `FILTER_REJECTED_TRADES_MAX_ROWS` rows, the oldest rows
are deleted until the count reaches 90% of the limit. Pruning runs
automatically after each insert.

### Post-hoc review

Query rejected trades to inspect filter decisions:

```sql
-- Most recent rejections
SELECT id, rejection_reason, filtered_at
FROM filtered_trades
ORDER BY filtered_at DESC
LIMIT 100;

-- Top rejection reasons
SELECT rejection_reason, COUNT(*) AS cnt
FROM filtered_trades
GROUP BY rejection_reason
ORDER BY cnt DESC;

-- Rejections in the last hour
SELECT COUNT(*) FROM filtered_trades
WHERE filtered_at > datetime('now', '-1 hour');
```

---

## Observability

Each filter tracks `rejection_count` (trades rejected since last reset). The
`TradeFilterPipeline.stats()` method returns a `{filter_name: count}` dict
that feeds into Prometheus metrics (see [docs/metrics.md](metrics.md)).

```python
pipeline.stats()
# {
#   "asset_pair_whitelist": 0,
#   "asset_pair_blacklist": 42,
#   "minimum_volume": 1205,
#   "asset_type": 88,
#   "account_exclusion": 13,
# }
```

---

## Tuning guidance

### Development

Keep all filters disabled (`enabled: false`) or use empty lists. This ensures
all trades reach the detection engine during local testing.

### Staging

Enable the minimum-volume filter with a conservative threshold (`"0.001"`).
Use a small whitelist of the top pairs by volume to bound resource consumption.

### Production (narrowly scoped)

```yaml
filters:
  - type: asset_pair_whitelist
    enabled: true
    pairs: [<top-20 pairs by monthly volume>]

  - type: asset_pair_blacklist
    enabled: true
    pairs: [TEST/XLM, DEMO/USDC]   # known spam

  - type: minimum_volume
    enabled: true
    min_volume: "0.01"

  - type: asset_type
    enabled: true
    allowed_types: [native, credit_alphanum4]

  - type: account_exclusion
    enabled: true
    excluded_accounts:
      - <verified market maker keys>
```

### Production (broad coverage)

Disable the whitelist entirely (set `enabled: false` or leave `pairs: []`).
Use only the blacklist, minimum-volume, and account-exclusion filters to cut
noise without restricting coverage.

---

## API reference

```python
from ingestion.filters import (
    TradeFilterPipeline,
    FilterConfigLoader,
    load_pipeline_from_config,
    AssetPairWhitelistFilter,
    AssetPairBlacklistFilter,
    MinimumVolumeFilter,
    AssetTypeFilter,
    AccountExclusionFilter,
    FilterResult,
    TradeFilter,
)
from detection.storage import store_filtered_trade, prune_filtered_trades

# Start pipeline with hot-reload watcher
pipeline, loader = load_pipeline_from_config("config/filter_config.yaml")

# Apply to each ingested trade
result = pipeline.apply(trade)
if not result.passed and settings.filter_store_rejected_trades:
    store_filtered_trade(trade, result.reason)

# Inspect stats
print(pipeline.stats())

# Shutdown
loader.stop()
```
