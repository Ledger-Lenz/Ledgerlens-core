# Cleanup Tasks

## Issue #421 — `api/metrics.py` ✅
- [x] Remove unused `from __future__ import annotations`
- [x] Narrow `get_overdue_count()` exception handling
- [x] Clean up imports, keep `metrics_response()` since tests depend on it

## Issue #418 — `api/graphql_schema.py` ✅
- [x] Import `secrets`, `settings`, `lookup_key` at top
- [x] Fix `_require_scope` to actually validate scope via `lookup_key`
- [x] Fix `_require_admin` to validate against `settings.admin_api_key`
- [x] Move late imports to top level
- [x] Add auth to `score` field resolver
- [x] Add logging for auth failures
- [x] Add error handling for storage lookups

## Issue #419 — `api/grpc_scoring_service.py` ✅
- [x] Fix `AuthInterceptor` to actually intercept and authenticate
- [x] Replace monkey-patched private attributes with proper context details
- [x] Handle admin keys from settings in `_authenticate`
- [x] Add proper type annotations to `BatchScoreWallets`
- [x] Use direct attribute access for `settings.grpc_max_batch_wallets`

## Issue #420 — `api/main.py` ✅
- [x] Remove ALL duplicate imports
- [x] Remove unused `_OPENAPI_TAGS` variable
- [x] Rename second `/feedback` endpoint path to `/feedback/ground-truth`
- [x] Consolidate sliding-window rate limiter into shared helper
- [x] Remove unused imports (`Response`, `APIRouter`, `asynccontextmanager`, `FutureTimeoutError`)
- [x] Fix `get_slo_status_from_registry` to log on ImportError
- [x] Update tests if needed

