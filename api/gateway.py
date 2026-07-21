"""Consolidated gateway middleware for auth, quota enforcement, and access logging.

Replaces the previous three-way duplication between:
- ``api/auth.py`` (single admin key check)
- ``api/api_key_router.py`` (scoped API key with ``detection/api_key_store.py``)
- ``api/api_keys_router.py`` (independent duplicate scoped API key system)
- ``api/namespace.py`` (namespace-level key handling)

Every authenticated request flows through :class:`GatewayMiddleware` once,
resolving the caller's identity, scope, and quota before the route handler runs.

See ``docs/api_gateway.md`` for architecture and migration guide.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config.settings import settings

logger = logging.getLogger("ledgerlens.gateway")

try:
    from api.metrics import gateway_requests_total
except ImportError:
    gateway_requests_total = None

# ---------------------------------------------------------------------------
# Route scope annotation
# ---------------------------------------------------------------------------

SCOPE_ANNOTATION_KEY = "required_scope"


def _path_matches(pattern: str, path: str) -> bool:
    """Check whether *path* matches a route *pattern* containing ``{param}`` segments."""
    regex = re.sub(r"\{[^}]+\}", r"[^/]+", pattern)
    return bool(re.fullmatch(regex, path))


def ann(request: Request, routes: list | None = None) -> str | None:
    """Read the required_scope annotation from the matched route.

    Checks (in order):
    1. ``request.scope["route"].required_scope`` (set by ``ScopedAPIRoute``)
    2. ``request.scope["route"].endpoint.__scoped_route__`` (set by ``@ScopedRoute``)
    3. When *routes* is provided and scope lookup fails, attempts manual
       path-based matching against each route's ``endpoint.__scoped_route__``.

    Returns None for unauthenticated (public) routes.
    """

    def _check_route(route_obj) -> str | None:
        required = getattr(route_obj, "required_scope", None)
        if required:
            return required
        endpoint = getattr(route_obj, "endpoint", None)
        if endpoint is not None:
            return getattr(endpoint, "__scoped_route__", None)
        return None

    route = request.scope.get("route")
    if route is not None:
        result = _check_route(route)
        if result is not None:
            return result

    # Fallback: manually match routes (for middleware dispatch where
    # request.scope["route"] is not yet populated).
    if routes is not None:
        path = request.url.path
        method = request.method.upper()
        for r in routes:
            rpath = getattr(r, "path", None)
            rmethods = getattr(r, "methods", None)
            if rpath is None or rmethods is None:
                continue
            if method not in {m.upper() for m in rmethods}:
                continue
            if not _path_matches(rpath, path):
                continue
            result = _check_route(r)
            if result is not None:
                return result

    return None


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------

def _resolve_auth(request: Request) -> dict | None:
    """Resolve the request's authentication to a key metadata dict.

    Checks, in order:
    1. ``X-LedgerLens-Admin-Key`` header — matched against ``LEDGERLENS_ADMIN_API_KEY``
    2. ``X-LedgerLens-Api-Key`` header — looked up in canonical ``api_keys`` store
    3. ``X-LedgerLens-Compliance-Key`` header — matched against ``LEDGERLENS_COMPLIANCE_API_KEY``

    Returns None if no valid key is found.
    """
    from detection.api_key_store import get_api_key_by_hash

    # 1. Admin key
    admin_key = request.headers.get("x-ledgerlens-admin-key", "")
    if admin_key and settings.admin_api_key:
        if secrets.compare_digest(admin_key, settings.admin_api_key):
            return {
                "key_id": "__admin__",
                "key_hash": "",
                "namespace_id": "*",
                "scopes": "admin",
                "rate_limit_per_minute": 0,
                "daily_quota": 0,
                "namespace_daily_quota": 0,
                "auth_type": "admin_key",
            }

    # 2. Compliance key
    compliance_key = request.headers.get("x-ledgerlens-compliance-key", "")
    if compliance_key and settings.compliance_api_key:
        if secrets.compare_digest(compliance_key, settings.compliance_api_key):
            return {
                "key_id": "__compliance__",
                "key_hash": "",
                "namespace_id": "*",
                "scopes": "compliance:read",
                "rate_limit_per_minute": 0,
                "daily_quota": 0,
                "namespace_daily_quota": 0,
                "auth_type": "compliance_key",
            }

    # 3. Scoped API key
    api_key = request.headers.get("x-ledgerlens-api-key", "")
    if not api_key:
        return None

    key_hash = hashlib.blake2b(api_key.encode(), digest_size=32).hexdigest()
    record = get_api_key_by_hash(key_hash)
    if record is None:
        return None

    record["auth_type"] = "api_key"
    return record


def _check_scope(required_scope: str | None, key_meta: dict) -> bool:
    """Check that the key has the required scope.

    ``admin`` scope grants access to everything.
    Returns True when allowed, False when denied.
    """
    if required_scope is None:
        return True
    scopes = set(s.strip() for s in key_meta.get("scopes", "").split(",") if s.strip())
    if required_scope in scopes or "admin" in scopes:
        return True
    return False


def _check_quota(key_meta: dict) -> tuple[bool, dict]:
    """Enforce per-key and per-namespace quota (daily + monthly + per-minute).

    Returns (allowed, headers_dict).
    When not allowed, headers_dict contains Retry-After and/or X-LedgerLens-Quota-Reset.
    """
    from detection.api_key_store import (
        check_daily_quota,
        check_namespace_quota,
        check_monthly_quota,
        check_namespace_monthly_quota,
        check_rate_limit,
    )

    key_id = key_meta["key_id"]
    namespace_id = key_meta.get("namespace_id", "")

    # Per-minute rate limit (checked first — fastest to fail). Shared with the
    # gRPC path and the legacy require_scope dependency via the same
    # distributed (Redis-backed) counter — see detection/rate_limiter.py.
    rate_limit = key_meta.get("rate_limit_per_minute", 0)
    if rate_limit > 0:
        allowed, retry_after = check_rate_limit(key_id, rate_limit)
        if not allowed:
            return False, {"Retry-After": str(retry_after)}

    # Daily quota per key
    daily_limit = key_meta.get("daily_quota", 0)
    if daily_limit > 0:
        allowed, reset_at = check_daily_quota(key_id, daily_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    # Daily quota per namespace (wildcard '*' is exempt)
    ns_daily_limit = key_meta.get("namespace_daily_quota", 0)
    if ns_daily_limit > 0 and namespace_id != "*":
        allowed, reset_at = check_namespace_quota(namespace_id, ns_daily_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    # Monthly quota per key
    monthly_limit = key_meta.get("monthly_quota", 0)
    if monthly_limit > 0:
        allowed, reset_at = check_monthly_quota(key_id, monthly_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    # Monthly quota per namespace (wildcard '*' is exempt)
    ns_monthly_limit = key_meta.get("namespace_monthly_quota", 0)
    if ns_monthly_limit > 0 and namespace_id != "*":
        allowed, reset_at = check_namespace_monthly_quota(namespace_id, ns_monthly_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    return True, {}


# ---------------------------------------------------------------------------
# Access logging
# ---------------------------------------------------------------------------


def _log_access(
    request: Request,
    response: Response,
    key_meta: dict | None,
    latency_ms: float,
    required_scope: str | None,
) -> None:
    """Log one structured access record per request.

    Never logs request/response bodies (PII/wallet exposure).
    """
    from detection.api_key_store import log_gateway_request

    key_id = key_meta.get("key_id", "") if key_meta else ""
    namespace_id = key_meta.get("namespace_id", "") if key_meta else ""

    log_gateway_request(
        key_id=key_id,
        namespace_id=namespace_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency_ms,
        scope=required_scope or "public",
    )

    # Emit Prometheus counter
    _emit_gateway_metric(namespace_id or "none", required_scope or "public", str(response.status_code))

    # Also emit a structured log line (no bodies, no wallet addresses)
    correlation_id = getattr(request.state, "correlation_id", "-")
    logger.info(
        "gateway method=%s path=%s status=%d latency_ms=%.1f key_id=%.8s namespace=%s scope=%s correlation_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        latency_ms,
        key_id,
        namespace_id or "-",
        required_scope or "public",
        correlation_id,
    )


def _emit_gateway_metric(namespace: str, scope: str, status: str) -> None:
    """Increment the gateway requests Prometheus counter."""
    if gateway_requests_total is not None:
        try:
            gateway_requests_total.labels(namespace=namespace, scope=scope, status=status).inc()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Gateway middleware
# ---------------------------------------------------------------------------


def _resolve_routes(app) -> list:
    """Walk the ASGI middleware stack to find the root router's routes."""
    visited = set()
    current = app
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        routes = getattr(current, "routes", None)
        if routes is not None:
            return routes
        inner = getattr(current, "app", None)
        if inner is not None:
            current = inner
        else:
            break
    return []


class GatewayMiddleware(BaseHTTPMiddleware):
    """Single point of auth resolution, quota enforcement, and access logging.

    Replaces per-router ``Depends(require_scope(...))`` calls with route
    metadata (``route.required_scope``, resolved via :func:`ann`) evaluated
    once here.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not settings.gateway_enabled:
            return await call_next(request)

        required_scope = ann(request, _resolve_routes(self.app))
        _start = time.monotonic()
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id

        # Public route — no auth required
        if required_scope is None:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            latency_ms = (time.monotonic() - _start) * 1000
            _log_access(request, response, None, latency_ms, None)
            return response

        # Resolve auth
        key_meta = _resolve_auth(request)
        if key_meta is None:
            resp = JSONResponse(
                {"detail": "Unauthorized — provide a valid API key, admin key, or compliance key"},
                status_code=401,
            )
            resp.headers["X-Correlation-ID"] = correlation_id
            return resp

        # Scope check
        if not _check_scope(required_scope, key_meta):
            resp = JSONResponse(
                {"detail": f"Scope '{required_scope}' required"},
                status_code=403,
            )
            resp.headers["X-Correlation-ID"] = correlation_id
            return resp

        # Quota enforcement
        allowed, quota_headers = _check_quota(key_meta)
        if not allowed:
            quota_headers["X-Correlation-ID"] = correlation_id
            return JSONResponse(
                {"detail": "Quota exceeded"},
                status_code=429,
                headers=quota_headers,
            )

        # Forward resolved key metadata for downstream handlers
        request.state.auth_key_meta = key_meta

        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        except Exception:
            response = JSONResponse({"detail": "Backend error"}, status_code=503)
            response.headers["X-Correlation-ID"] = correlation_id
            raise
        finally:
            latency_ms = (time.monotonic() - _start) * 1000
            _log_access(request, response, key_meta, latency_ms, required_scope)


# ---------------------------------------------------------------------------
# Route-annotation helpers
# ---------------------------------------------------------------------------


class ScopedRoute:
    """Descriptor that attaches a ``required_scope`` to a route handler.

    Usage::

        @router.get("/scores/{wallet}")
        @ScopedRoute("read:scores")
        async def get_wallet_scores(wallet: str): ...
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def __call__(self, func):
        func.__scoped_route__ = self.scope
        return func


def _resolve_required_scope(request: Request) -> str | None:
    """Return the required scope for a request's matched route.

    Checks (in order):
    1. ``route.required_scope`` attribute (set by ``ScopedAPIRoute``)
    2. ``route.endpoint.__scoped_route__`` (set by ``@ScopedRoute`` decorator)
    3. Endpoint's ``dependencies`` list for any ``require_scope`` or
       ``require_admin_key`` dependency

    Returns None for public (unauthenticated) routes.
    """
    route = request.scope.get("route")
    if route is None:
        return None

    # Check the custom attribute first
    required = getattr(route, "required_scope", None)
    if required:
        return required

    # Check decorator annotation on the endpoint
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        required = getattr(endpoint, "__scoped_route__", None)
        if required:
            return required

    # Infer from dependencies (backward compat)
    deps = getattr(route, "dependencies", [])
    for dep in deps:
        dep_callable = getattr(dep, "dependency", None) or dep
        dep_name = getattr(dep_callable, "__name__", "") or getattr(dep_callable, "__class__", "").__name__
        dep_name = str(dep_name)
        if "require_admin_key" in dep_name:
            return "admin"
        if "require_compliance_key" in dep_name:
            return "compliance:read"
        if "require_scope" in dep_name:
            scope = getattr(dep_callable, "__closure__", None)
            if scope:
                for cell in scope:
                    if isinstance(cell.cell_contents, str):
                        return cell.cell_contents
            return "read:scores"

    return None


def scope_required(scope: str):
    """Decorator that marks a route handler as requiring a scope.

    Example::

        @router.get("/admin/scores")
        @scope_required("admin")
        async def admin_scores(): ...
    """
    def decorator(func):
        func.__scoped_route__ = scope
        return func
    return decorator


# ---------------------------------------------------------------------------
# Middleware adapter for existing routers
# ---------------------------------------------------------------------------

_scoped_endpoints: dict[str, str] = {}


def register_scoped_endpoint(path: str, scope: str) -> None:
    """Register a path + method as requiring a specific scope.

    Used by deprecated ``api/api_keys_router.py`` and ``api/api_key_router.py``
    during the transition period.
    """
    _scoped_endpoints[path] = scope