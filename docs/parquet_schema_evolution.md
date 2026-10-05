# Parquet Schema Evolution Policy

Files written by `ingestion/parquet_exporter.py` must stay readable by current and
future tooling without per-version handling.

## Discoverable version

Every exported file carries its schema version in Parquet key-value metadata under
`ledgerlens.schema_version` (also recorded in `manifest.json`):

```python
from ingestion.parquet_exporter import read_schema_version, read_parquet_compatible

read_schema_version("trades.parquet")      # "1.0"
table = read_parquet_compatible("trades.parquet")  # always the current schema
```

Files written before the tag existed are reported as `LEGACY_SCHEMA_VERSION` (`1.0`).

## Rules for contributors (compatible-by-default)

Follow Avro-style backward/forward compatibility rules:

1. **Add fields only as nullable columns.** Older files are read with the new column
   filled with nulls; older readers ignore unknown columns.
2. **Never change a column's type** except to a strictly wider one that
   `pyarrow.cast` handles losslessly (e.g. larger decimal precision).
3. **Never reuse a removed column name** for different data.
4. **Removing a field**: drop it from the schema; `read_parquet_compatible` drops
   unknown columns from older files.
5. **Renaming a field**: add `{old_name: new_name}` to `FIELD_RENAMES`; never
   delete a rename entry.
6. **Every schema change** bumps `SCHEMA_VERSION` and appends (never edits) an entry
   in `SCHEMA_REGISTRY`. Minor bump for additive changes, major bump for
   removals/renames.
7. **Add a fixture** written with the previous version to `tests/fixtures/parquet/`
   and extend `tests/test_parquet_schema_evolution.py` to read it with current tooling.
