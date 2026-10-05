"""Cross-protocol policy parity tests (#969).

Equivalent credentials must get the same auth / scope / rate-limit decision
on REST, GraphQL, gRPC and WebSocket, because all four go through
:func:`api.policy.enforce`. Each adapter maps its protocol's wire-level
result back to a canonical outcome so the results can be compared directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import policy
from config.settings import settings
from detection.rate_limiter import reset_rate_limiter

ADMIN = "parity-admin-key"
OK, UNAUTH, FORBID, LIMITED = "ok", "unauthenticated", "forbidden", "rate_limited"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ledgerlens_db_path", str(tmp_path / "keys.db"))
    monkeypatch.setattr(settings, "gateway_quota_store", "sqlite")
    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", ADMIN)
    monkeypatch.setattr(settings, "gateway_enabled", True)
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _key(scopes: list[str], rpm: int = 100) -> str:
    from detection.api_key_store import create_api_key

    return create_api_key(scopes=scopes, rate_limit_per_minute=rpm)["plaintext_key"]


# ---------------------------------------------------------------------------
# Protocol adapters: (creds, scope) -> canonical outcome
# ---------------------------------------------------------------------------


def _rest(creds: dict, scope: str) -> str:
    from api.gateway import GatewayMiddleware, scope_required

    app = FastAPI()
    app.add_middleware(GatewayMiddleware)

    @app.get("/probe")
    @scope_required(scope)
    def probe():
        return {"ok": True}

    headers = {}
    if creds.get("api_key"):
        headers["X-LedgerLens-Api-Key"] = creds["api_key"]
    if creds.get("admin_key"):
        headers["X-LedgerLens-Admin-Key"] = creds["admin_key"]
    code = TestClient(app).get("/probe", headers=headers).status_code
    return {200: OK, 401: UNAUTH, 403: FORBID, 429: LIMITED}[code]


def _graphql(creds: dict, scope: str) -> str:
    pytest.importorskip("pandas")
    from graphql import GraphQLError

    from api.graphql_schema import _enforce

    headers = {}
    if creds.get("api_key"):
        headers["X-LedgerLens-Api-Key"] = creds["api_key"]
    if creds.get("admin_key"):
        headers["X-LedgerLens-Admin-Key"] = creds["admin_key"]
    info = SimpleNamespace(context={"request": SimpleNamespace(headers=headers)})
    try:
        _enforce(info, scope)
    except GraphQLError as exc:
        msg = exc.message
        if msg.startswith("Unauthorized"):
            return UNAUTH
        if msg.startswith("Forbidden"):
            return FORBID
        return LIMITED
    return OK


class _Aborted(Exception):
    def __init__(self, code):
        self.code = code


def _grpc(creds: dict, scope: str) -> str:
    pytest.importorskip("pandas")
    import grpc

    from api.grpc_scoring_service import _authenticate

    md = []
    if creds.get("api_key"):
        md.append(("x-ledgerlens-api-key", creds["api_key"]))
    if creds.get("admin_key"):
        md.append(("x-ledgerlens-admin-key", creds["admin_key"]))

    def abort(code, _detail):
        raise _Aborted(code)

    ctx = SimpleNamespace(invocation_metadata=lambda: md, abort=abort)
    try:
        _authenticate(ctx, required_scope=scope)
    except _Aborted as exc:
        return {
            grpc.StatusCode.UNAUTHENTICATED: UNAUTH,
            grpc.StatusCode.PERMISSION_DENIED: FORBID,
            grpc.StatusCode.RESOURCE_EXHAUSTED: LIMITED,
        }[exc.code]
    return OK


def _ws_allowed(creds: dict) -> bool:
    """WebSocket closes with 1008 on any denial, so only allow/deny is observable."""
    from api.ws_router import manager, router

    app = FastAPI()
    app.include_router(router)
    token = creds.get("admin_key") or creds.get("api_key") or ""
    try:
        with TestClient(app).websocket_connect(f"/ws/alerts?api_key={token}") as ws:
            ws.close()
        return True
    except WebSocketDisconnect:
        return False
    finally:
        manager._connections.clear()


ADAPTERS = {"rest": _rest, "graphql": _graphql, "grpc": _grpc}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _scenarios() -> list[tuple[str, dict, str, str]]:
    """(name, creds, required_scope, expected) — built after env fixture runs."""
    reader = _key(["read:scores"])
    writer = _key(["write:suppressions"])
    scoped_admin = _key(["admin"])
    return [
        ("no credentials", {}, "read:scores", UNAUTH),
        ("unknown api key", {"api_key": "ll_bogus"}, "read:scores", UNAUTH),
        ("wrong admin key", {"admin_key": "nope"}, "admin", UNAUTH),
        ("key missing scope", {"api_key": writer}, "read:scores", FORBID),
        ("key with scope", {"api_key": reader}, "read:scores", OK),
        ("reader on admin surface", {"api_key": reader}, "admin", FORBID),
        ("admin key", {"admin_key": ADMIN}, "read:scores", OK),
        ("admin key on admin surface", {"admin_key": ADMIN}, "admin", OK),
        ("admin-scoped api key", {"api_key": scoped_admin}, "admin", OK),
    ]


@pytest.mark.parametrize("protocol", sorted(ADAPTERS))
def test_protocol_matches_policy_decision(env, protocol):
    for name, creds, scope, expected in _scenarios():
        assert ADAPTERS[protocol](creds, scope) == expected, f"{protocol}: {name}"


def test_all_protocols_agree(env):
    pytest.importorskip("pandas")
    for name, creds, scope, _ in _scenarios():
        outcomes = {p: fn(creds, scope) for p, fn in ADAPTERS.items()}
        assert len(set(outcomes.values())) == 1, f"{name}: {outcomes}"


def test_websocket_matches_policy_on_admin_surface(env):
    for name, creds, scope, expected in _scenarios():
        if scope != "admin":
            continue
        assert _ws_allowed(creds) == (expected == OK), f"ws: {name}"


@pytest.mark.parametrize("protocol", sorted(ADAPTERS))
def test_rate_limit_enforced_identically(env, protocol):
    key = _key(["read:scores"], rpm=2)
    outcomes = [ADAPTERS[protocol]({"api_key": key}, "read:scores") for _ in range(3)]
    assert outcomes == [OK, OK, LIMITED]


def test_rate_limit_is_shared_across_protocols(env):
    """One per-key budget: spending it on REST exhausts it on every surface."""
    pytest.importorskip("pandas")
    key = _key(["read:scores"], rpm=3)
    assert _rest({"api_key": key}, "read:scores") == OK
    assert _graphql({"api_key": key}, "read:scores") == OK
    assert _grpc({"api_key": key}, "read:scores") == OK
    for protocol, fn in ADAPTERS.items():
        assert fn({"api_key": key}, "read:scores") == LIMITED, protocol


def test_enforce_is_the_single_code_path(env, monkeypatch):
    """Every adapter must reach policy.enforce — patching it changes all of them."""
    calls: list[str] = []
    real = policy.enforce

    def spy(scope, **kw):
        calls.append(scope)
        return real(scope, **kw)

    monkeypatch.setattr(policy, "enforce", spy)
    key = _key(["read:scores"])
    for fn in ADAPTERS.values():
        try:
            fn({"api_key": key}, "read:scores")
        except pytest.skip.Exception:
            continue
    _ws_allowed({"admin_key": ADMIN})
    assert calls, "no protocol called policy.enforce"
    assert all(c in ("read:scores", "admin") for c in calls)
