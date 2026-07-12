# TODO - SQL paging (limit/offset)

**Status: done.** `detection/storage.py:get_latest_scores()` accepts `limit`/`offset`
and pages in SQL via `LIMIT ? OFFSET ?`; `api/main.py`'s `list_scores` endpoint
validates both with FastAPI `Query(...)` (422 on out-of-range values); coverage
lives in `tests/test_storage.py` and `tests/test_api.py`.

See [ROADMAP.md](ROADMAP.md) for what's tracked next.
