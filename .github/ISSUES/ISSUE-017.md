---
title: "Build a Configurable Trade Filter Pipeline Before Detection Ingestion"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
LedgerLens currently ingests all trades from Horizon and passes them all to the detection engine, including dust trades, test asset pairs, and known-clean institutional market makers that would add noise to the risk models without contributing to wash-trade detection. A configurable filter pipeline — supporting asset pair whitelists/blacklists, minimum volume thresholds, asset type filters, and account-level exclusion lists — will reduce detection noise, lower compute costs, and allow operators to focus analysis on the asset pairs and volume tiers where wash trading is most impactful.

## Background & Context
The ingestion layer (Layer 1 in the README architecture) feeds all ingested trades directly into `detection/feature_engineering.py`. In production, the SDEX processes thousands of unique asset pairs, many of which are:
- **Dust/spam assets**: tiny asset pairs created by bots or accidentally, with sub-cent total volume
- **Test pairs**: asset codes like `TEST`, `DEMO`, or pairs with known test issuers
- **Excluded accounts**: known clean market makers, official Stellar Foundation accounts, DEX aggregator bots that have been manually verified
- **Unsupported asset types**: `credit_alphanum12` assets with complex codes not relevant to the target detection scope

Processing these unnecessary records adds CPU/memory load to the feature engineering pipeline and can introduce false positives (a legitimate high-frequency market maker that happens to look anomalous on Benford's Law).

The filter pipeline must be:
1. **Configurable**: rules defined in a YAML/JSON config file (not hardcoded), reloadable without restart
2. **Composable**: multiple filter rules applied in sequence (logical AND — a trade passes only if it passes all active filters)
3. **Observable**: each filter records how many trades it rejected, for use in metrics (ISSUE-015)
4. **Non-destructive**: rejected trades are not discarded but sent to a `filtered` status in SQLite so they can be reviewed and the filter rules adjusted

## Objectives
- [ ] Implement a `TradeFilterPipeline` class in a new `ingestion/filters.py` module that applies an ordered list of `TradeFilter` instances to each incoming `Trade` object and returns a `FilterResult` indicating pass/reject with reason.
- [ ] Implement five concrete filter classes: `AssetPairWhitelistFilter`, `AssetPairBlacklistFilter`, `MinimumVolumeFilter`, `AssetTypeFilter`, and `AccountExclusionFilter`.
- [ ] Add a `filter_config.yaml` configuration file (with schema validation) that operators edit to configure the active filters, and implement hot-reload so changes take effect within 60 seconds without restarting.
- [ ] Store rejected trades in a `filtered_trades` SQLite table with the rejection reason, enabling post-hoc review and filter rule adjustment.

## Technical Requirements

**`TradeFilter` base class:**
```python
from abc import ABC, abstractmethod

class FilterResult:
    passed: bool
    reason: str | None = None    # populated when passed=False

class TradeFilter(ABC):
    name: str   # human-readable filter name, used in metrics and logs

    @abstractmethod
    def apply(self, trade: Trade) -> FilterResult: ...

    @property
    def rejection_count(self) -> int: ...

    def reset_stats(self) -> None: ...
```

**`AssetPairWhitelistFilter`**: if a whitelist is non-empty, only allow trades where `"{base_asset_code}/{counter_asset_code}"` is in the whitelist set. Empty whitelist = allow all.
```python
class AssetPairWhitelistFilter(TradeFilter):
    name = "asset_pair_whitelist"
    def __init__(self, allowed_pairs: set[str]): ...
```

**`AssetPairBlacklistFilter`**: reject trades where the asset pair is in the blacklist.
```python
class AssetPairBlacklistFilter(TradeFilter):
    name = "asset_pair_blacklist"
    def __init__(self, blocked_pairs: set[str]): ...
```

**`MinimumVolumeFilter`**: reject trades where `base_amount < min_volume_xlm` (after converting to XLM equivalent using a configurable price oracle or a simple pass-through with the raw amount if both assets are not XLM-denominated).
```python
class MinimumVolumeFilter(TradeFilter):
    name = "minimum_volume"
    def __init__(self, min_volume: Decimal, volume_field: str = "base_amount"): ...
```

**`AssetTypeFilter`**: reject trades involving assets of certain types (e.g., reject all `credit_alphanum12` if only 4-char codes are in scope).
```python
class AssetTypeFilter(TradeFilter):
    name = "asset_type"
    def __init__(self, allowed_types: set[Literal["native", "credit_alphanum4", "credit_alphanum12"]]): ...
```

**`AccountExclusionFilter`**: reject trades where either `base_account` or `counter_account` is in the exclusion set.
```python
class AccountExclusionFilter(TradeFilter):
    name = "account_exclusion"
    def __init__(self, excluded_accounts: set[str]): ...
```

**`TradeFilterPipeline`:**
```python
class TradeFilterPipeline:
    def __init__(self, filters: list[TradeFilter]): ...

    def apply(self, trade: Trade) -> FilterResult:
        for f in self.filters:
            result = f.apply(trade)
            if not result.passed:
                return FilterResult(passed=False, reason=f"{f.name}: {result.reason}")
        return FilterResult(passed=True)

    def stats(self) -> dict[str, int]:
        """Return {filter_name: rejection_count} for all filters."""
```

**`filter_config.yaml` schema:**
```yaml
version: "1.0"
filters:
  - type: asset_pair_whitelist
    enabled: true
    pairs:
      - "XLM/USDC"
      - "XLM/BTC"
      - "USDC/BTC"

  - type: asset_pair_blacklist
    enabled: true
    pairs:
      - "TEST/XLM"
      - "SPAM/USDC"

  - type: minimum_volume
    enabled: true
    min_volume: "0.01"
    volume_field: "base_amount"

  - type: asset_type
    enabled: true
    allowed_types: ["native", "credit_alphanum4"]

  - type: account_exclusion
    enabled: true
    excluded_accounts:
      - "GCEZWKCA5VLDNRLN3RPRJMRZOX3Z6G5CHCGCHBZVM1PBWQ5FIWM77V4"  # Stellar Foundation
```

**Hot-reload implementation**: `FilterConfigLoader` uses `watchdog` or periodic `stat()` to detect changes to `filter_config.yaml`. On change, it re-parses and validates the YAML, and atomically replaces the `filters` list on `TradeFilterPipeline`. Use `threading.Lock` around the filter list swap to prevent races with in-flight `apply()` calls.

**`filtered_trades` SQLite table:**
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
Store only the primary key fields and rejection reason — not the full trade record — to keep the table small.

**Configuration** (add to `config/settings.py`):
- `FILTER_CONFIG_PATH`: default `./config/filter_config.yaml`
- `FILTER_CONFIG_RELOAD_INTERVAL_SECONDS`: default `60`
- `FILTER_STORE_REJECTED_TRADES`: default `True`
- `FILTER_REJECTED_TRADES_MAX_ROWS`: default `500_000` (prune to 450k when exceeded)

## Security Considerations
- The `filter_config.yaml` file must be validated against a strict schema (using `pydantic` or `jsonschema`) on every load — a malformed config must not silently disable all filters (fail-safe: if config is invalid, retain the previous valid config and log an `ERROR`).
- The `account_exclusion` filter list is sensitive — it reveals which accounts are considered "known clean" market makers. Protect the `filter_config.yaml` with appropriate file permissions (`0o640`) and document that it may contain operationally sensitive data.
- Hot-reload must be atomic: the old filter list must remain active until the new list is fully validated and ready — never leave the pipeline in a state with no filters during the swap.
- Account IDs in `excluded_accounts` must be validated as valid Stellar public keys on config load to catch typos that would silently fail to exclude the intended account.
- SQL inserts for `filtered_trades` must use parameterised queries — `rejection_reason` is derived from filter logic and should not be user-controlled, but parameterisation is still required as a defensive practice.

## Testing Requirements
- Unit tests covering each `TradeFilter.apply()`: a trade that passes, a trade that fails, edge cases specific to each filter (empty whitelist = allow all, empty blacklist = allow all, `min_volume=0` = allow all)
- Unit tests covering `TradeFilterPipeline.apply()`: first filter rejects (short-circuit), all filters pass, second filter rejects
- Unit tests covering `FilterConfigLoader`: valid YAML loaded correctly, invalid YAML retains previous config, new filters activated after hot-reload
- Unit tests covering `AccountExclusionFilter`: valid Stellar address excluded, invalid address at config load time (ValueError)
- Integration tests: mock `HorizonStreamer` emitting 100 trades (50 on whitelist, 50 not); run through `TradeFilterPipeline`; assert 50 pass, 50 in `filtered_trades` table
- Integration tests: modify `filter_config.yaml` mid-test; assert new filter takes effect within `FILTER_CONFIG_RELOAD_INTERVAL_SECONDS`
- Edge cases: trade with `base_asset_code=None` (native XLM trade) against an asset pair whitelist, empty pipeline (no filters — all trades pass), concurrent hot-reload and active apply calls
- Performance benchmark: filtering 10,000 trades through a 5-filter pipeline should complete in < 100 ms

## Documentation Requirements
- Create `config/filter_config.yaml.example` with commented examples of all filter types
- Update `README.md` CLI Reference and Quick Start to mention filter configuration
- Add docstrings to `TradeFilterPipeline`, `TradeFilter`, and each concrete filter class
- Create `docs/filter-pipeline.md` documenting the filter types, YAML schema, hot-reload behaviour, and guidance on tuning filters for different deployment environments

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: pipeline filter patterns, YAML schema validation, hot-reloadable configuration, Python threading, Stellar asset pair conventions
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with experience building configurable data filter pipelines. Familiarity with hot-reload configuration patterns, YAML schema validation (Pydantic or `jsonschema`), and thread-safe configuration swaps. Understanding of Stellar asset types and pair conventions is useful.
