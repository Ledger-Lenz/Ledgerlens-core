# TODO - SQL paging (limit/offset)

## Plan to implement
- [ ] Update `detection/storage.py:get_latest_scores()` signature to accept `limit: int | None = None` and `offset: int = 0`.
- [ ] Modify SQL to append `LIMIT ? OFFSET ?` when `limit` is provided; ensure paging is done in SQL.
- [ ] Update `api/main.py` endpoints `list_scores` and `alerts` to accept `limit` and `offset` query params with FastAPI validation.
- [ ] Ensure HTTP 422 behavior for out-of-range params.
- [ ] Thread `limit`/`offset` through `api/main.py` to `get_latest_scores()`.
- [ ] Update `tests/test_storage.py` to assert SQL includes `LIMIT ? OFFSET ?` when limit is set.
- [ ] Update `tests/test_api.py` for default paging, custom limit/offset, and invalid params.
- [ ] Run full test suite.

