"""Consolidated gateway middleware for auth, quota enforcement, and access logging.

Replaces the previous three-way duplication between:
- ``api/auth.py`` (single admin key check)
- ``api/api_key_router.py`` (scoped API key with ``detection/api_key_store.py``)
- ``api/api_keys_router.py`` (independent duplicate scoped API key system)
- ``api/namespace.py`` (namespace-level key handling)

Every authenticated request flows through :class:`GatewayMiddleware` once,
resolving the caller's identity, scope, and quota before the route handler runs.

See ``docs/api_gateway.md`` for architecture and migration guide, and
``docs/api_policy.md`` for the cross-protocol policy model.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api import policy
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
    2. ``request.scope["route"].endpoint.__scoped_route__`` (set by ``@scope_required``)
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
# Auth resolution — delegates to the shared policy layer (api/policy.py)
# ---------------------------------------------------------------------------

def _resolve_auth(request: Request) -> dict | None:
    """Resolve the request's admin / compliance / scoped API key headers."""
    return policy.resolve_credentials(
        admin_key=request.headers.get("x-ledgerlens-admin-key", ""),
        api_key=request.headers.get("x-ledgerlens-api-key", ""),
        compliance_key=request.headers.get("x-ledgerlens-compliance-key", ""),
    )


_check_scope = policy.check_scope
_check_quota = policy.check_quota


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

        decision = policy.enforce(
            required_scope,
            admin_key=request.headers.get("x-ledgerlens-admin-key", ""),
            api_key=request.headers.get("x-ledgerlens-api-key", ""),
            compliance_key=request.headers.get("x-ledgerlens-compliance-key", ""),
        )
        if not decision.allowed:
            status_code = {
                policy.UNAUTHENTICATED: 401,
                policy.FORBIDDEN: 403,
                policy.RATE_LIMITED: 429,
            }[decision.status]
            headers = {**decision.headers, "X-Correlation-ID": correlation_id}
            return JSONResponse({"detail": decision.detail}, status_code=status_code, headers=headers)
        key_meta = decision.key_meta

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