---
title: "Implement Full Pydantic v2 Migration for All Ingestion Data Models"
labels: ["difficulty: advanced", "area: ingestion", "type: chore"]
assignees: []
---

## Summary
`ingestion/data_models.py` uses Pydantic v1 validators (`@validator`, `@root_validator`) that are deprecated in Pydantic v2 and emit deprecation warnings in current environments. Migrating to Pydantic v2's `model_validator`, `field_validator`, and strict-mode configuration will eliminate deprecation noise, unlock significant performance improvements (Pydantic v2's Rust-based core is 5–50× faster than v1), and ensure long-term compatibility as v1 support is dropped upstream.

## Background & Context
`ingestion/data_models.py` defines the canonical Pydantic schemas for all ingestion records: `Trade`, `Asset`, `OrderBookEvent`, and related models. These schemas are used throughout the codebase:
- `ingestion/horizon_streamer.py` — deserializes SSE events into `Trade` objects
- `ingestion/historical_loader.py` — deserializes paginated REST responses
- `ingestion/operations_loader.py` — deserializes offer-create/update/cancel operations into `OrderBookEvent`
- `detection/feature_engineering.py` — receives `Trade` objects as inputs

Pydantic v2 introduced breaking API changes:
- `@validator` → `@field_validator` with `@classmethod` decorator and `mode='before'`/`mode='after'`
- `@root_validator` → `@model_validator` with `mode='before'`/`mode='after'`
- `class Config` → `model_config = ConfigDict(...)`
- `__fields__` → `model_fields`
- `.dict()` → `.model_dump()`
- `.json()` → `.model_dump_json()`
- `parse_obj` → `model_validate`
- `parse_raw` → `model_validate_json`

All call sites across the ingestion and detection modules must be updated simultaneously to avoid a mixed-mode state where some code calls `.dict()` (v1) and other code calls `.model_dump()` (v2) on the same objects.

The README lists `RiskScore` (defined in `detection/risk_score.py`) as a shared contract across repos. While `risk_score.py` is out of scope for this issue, the migration here must not change the serialised field names or types of `Trade`, `Asset`, or `OrderBookEvent` — only the internal validator syntax changes.

## Objectives
- [ ] Migrate all Pydantic models in `ingestion/data_models.py` from v1 `@validator`/`@root_validator` to v2 `@field_validator`/`@model_validator` with correct `mode` parameters and `@classmethod` decorators.
- [ ] Replace all `class Config` inner classes with `model_config = ConfigDict(...)` and enable `strict=False` (preserve existing coercion behaviour) plus `populate_by_name=True` where field aliases exist.
- [ ] Update all call sites across `ingestion/` and `detection/` that use `.dict()`, `.json()`, `parse_obj()`, `parse_raw()`, or `__fields__` to their v2 equivalents.
- [ ] Enable Pydantic v2 strict mode selectively on fields that must not be coerced (e.g., `paging_token: str` should never silently coerce an `int`), and document the mode choice per field.

## Technical Requirements

**Model config migration pattern:**
```python
# v1 (old)
class Trade(BaseModel):
    class Config:
        allow_population_by_field_name = True
        use_enum_values = True

# v2 (new)
from pydantic import ConfigDict

class Trade(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
    )
```

**Field validator migration pattern:**
```python
# v1 (old)
@validator("amount", pre=True)
def parse_amount(cls, v):
    return Decimal(str(v))

# v2 (new)
@field_validator("amount", mode="before")
@classmethod
def parse_amount(cls, v: Any) -> Decimal:
    return Decimal(str(v))
```

**Root validator migration pattern:**
```python
# v1 (old)
@root_validator
def check_asset_consistency(cls, values):
    ...
    return values

# v2 (new)
@model_validator(mode="after")
def check_asset_consistency(self) -> "Trade":
    ...
    return self
```

**`Trade` model — key fields and their v2 treatment:**
```python
class Trade(BaseModel):
    model_config = ConfigDict(populate_by_name=True, strict=False)

    id: str                                      # strict: must be str
    paging_token: str                            # strict: must be str
    ledger_close_time: datetime                  # coerce from ISO string
    base_account: str                            # strict: Stellar account ID (G...)
    counter_account: str
    base_amount: Decimal                         # coerce from str via field_validator
    counter_amount: Decimal
    base_asset_type: str
    base_asset_code: str | None = None
    base_asset_issuer: str | None = None
    counter_asset_type: str
    counter_asset_code: str | None = None
    counter_asset_issuer: str | None = None
    price: Decimal
    base_is_seller: bool
    trade_type: Literal["orderbook", "liquidity_pool"] = "orderbook"
```

**`Asset` model:**
```python
class Asset(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    asset_type: Literal["native", "credit_alphanum4", "credit_alphanum12"]
    asset_code: str | None = None
    asset_issuer: str | None = None

    @model_validator(mode="after")
    def validate_non_native_fields(self) -> "Asset":
        if self.asset_type != "native":
            if not self.asset_code or not self.asset_issuer:
                raise ValueError("Non-native assets require asset_code and asset_issuer")
        return self
```

**`OrderBookEvent` model** — migrate similarly, ensuring `offer_id` is validated as a positive integer.

**Call site audit** — run the following before and after to ensure no v1 call sites remain:
```bash
grep -rn '\.dict()\|parse_obj\|parse_raw\|__fields__\|@validator\|@root_validator\|class Config' ingestion/ detection/ api/ tests/
```
This grep must return zero matches after the migration.

**Type annotation tightening**: use `Annotated` with `Decimal` constraints where appropriate:
```python
from pydantic import condecimal
# v2 preferred:
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
```

**Performance target**: deserialising a batch of 1,000 `Trade` objects from JSON should complete in < 50 ms with Pydantic v2 (measured with `timeit`).

## Security Considerations
- Strict-mode fields (`paging_token`, `id`, account IDs) prevent silent type coercion that could allow crafted inputs to bypass validation by passing numeric values where strings are expected.
- All `@field_validator(mode="before")` functions must sanitise inputs before Pydantic processes them — do not trust raw Horizon API values to be the correct type.
- Ensure that `model_dump()` does not serialise `None` values for optional fields unless `exclude_none=False` is explicitly intended — use `model_dump(exclude_none=True)` at all call sites that write to SQLite to avoid storing null placeholders.
- The migration must not change the wire format of records persisted to SQLite or published to the API — run the existing test suite to confirm no serialisation regressions.

## Testing Requirements
- Unit tests covering each migrated model: valid construction from dict, invalid construction (missing required field, wrong type, constraint violation)
- Unit tests covering each `@field_validator` and `@model_validator`: test each validation rule independently with valid and invalid inputs
- Unit tests covering the `Asset` non-native consistency validator: native asset with no code (valid), non-native with missing code (invalid)
- Unit tests covering call site changes: `.model_dump()` returns correct dict shape, `.model_dump_json()` round-trips correctly
- Integration tests: deserialise a real Horizon API response fixture (stored in `tests/fixtures/`) through the updated models; assert field values are correct
- Edge cases: `Decimal` fields with scientific notation (`"1.5e-7"`), `datetime` fields with timezone offset vs Z suffix, `None` vs missing keys for optional fields, extra fields from future Horizon API versions (should be ignored)
- Performance benchmark: 1,000-object batch deserialisation < 50 ms

## Documentation Requirements
- Add a `MIGRATION_NOTES.md` or inline comment block in `data_models.py` documenting the v1→v2 migration decisions (why strict mode was chosen for specific fields, why certain fields remain non-strict)
- Update `requirements.txt` to pin `pydantic>=2.0,<3.0`
- Add docstrings to each model class and each validator explaining the validation logic and any non-obvious Pydantic v2 behaviour

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Pydantic v2 migration, Python type annotations, `Annotated`, `ConfigDict`, `model_validator`
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python engineer with hands-on Pydantic v2 migration experience on a production codebase. Familiarity with Pydantic's strict mode, `Annotated` types, and the v1→v2 API mapping is essential. Experience with the Stellar Horizon API response format is a plus but not required.
