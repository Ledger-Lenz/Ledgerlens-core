# Pydantic v2 migration

The ingestion contracts use native Pydantic v2 APIs and a shared
`ConfigDict(populate_by_name=True, strict=False, extra="ignore")`.

- Global non-strict mode is intentional. Horizon sends datetimes and numeric
  values as JSON strings, so those fields must remain coercible.
- Record IDs, paging tokens, account IDs, asset identifiers, transaction
  hashes, and pool IDs are strict strings. Numeric values cannot silently
  become identifiers.
- Numeric `mode="before"` validators reject booleans, non-finite values, and
  malformed text before constrained float or `Decimal` validation.
- Trade and path-payment amounts and prices must be positive. Order-book event
  amounts may be zero because zero represents cancellation.
- `OrderBookEvent.offer_id` is a strict positive integer when present. Horizon's
  zero create sentinel is represented as `None`, and the field is excluded from
  serialization to preserve the established public record shape.
- Unknown fields are ignored so records remain compatible with additive Horizon
  API changes.

Persisted and API-visible names are unchanged. Use `model_dump()`,
`model_dump_json()`, `model_validate()`, and `model_validate_json()` for all new
call sites.
