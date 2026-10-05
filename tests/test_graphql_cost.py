"""Tests for static GraphQL query-cost analysis (#970)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import strawberry
from graphql import parse

from api import policy
from api.graphql_cost import QueryCostLimiter, calculate_query_cost
from config.settings import settings
from detection.rate_limiter import reset_rate_limiter

resolved: list[str] = []


@strawberry.type
class Node:
    id: str

    @strawberry.field
    def neighbours(self) -> list["Node"]:
        resolved.append("neighbours")
        return [Node(id=f"{self.id}.{i}") for i in range(2)]


@strawberry.type
class Query:
    @strawberry.field
    def node(self, id: str) -> Node:
        resolved.append("node")
        return Node(id=id)


schema = strawberry.Schema(query=Query, extensions=[QueryCostLimiter])

CHEAP = '{ node(id: "a") { id neighbours { id } } }'  # 1 + 1 + (1 + 10 * 1) = 13
NESTED = (
    '{ node(id: "a") { neighbours { neighbours { neighbours { id } } } } }'
)  # 1 + 1 + 10*(1 + 10*(1 + 10*1)) = 1112


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ledgerlens_db_path", str(tmp_path / "keys.db"))
    monkeypatch.setattr(settings, "gateway_quota_store", "sqlite")
    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", "admin-secret")
    reset_rate_limiter()
    policy.set_tier_limits({})
    resolved.clear()
    yield
    policy.set_tier_limits({})


def _ctx(headers: dict | None = None) -> dict:
    return {"request": SimpleNamespace(headers=headers or {})}


def _key(tier: str) -> str:
    from detection.api_key_store import create_api_key

    return create_api_key(scopes=["read:scores"], tier=tier)["plaintext_key"]


def test_cost_counts_fields_and_list_multiplier():
    gql = schema._schema
    assert calculate_query_cost(gql, parse(CHEAP)).cost == 13
    result = calculate_query_cost(gql, parse(NESTED))
    assert result.cost == 1112
    assert result.depth == 5


def test_fragments_and_introspection_are_costed_correctly():
    gql = schema._schema
    frag = '{ node(id: "a") { ...F __typename } } fragment F on Node { id neighbours { id } }'
    assert calculate_query_cost(gql, parse(frag)).cost == 13
    inline = '{ node(id: "a") { ... on Node { id } } }'
    assert calculate_query_cost(gql, parse(inline)).cost == 2


def test_expensive_nested_query_rejected_before_execution(env):
    result = schema.execute_sync(NESTED, context_value=_ctx())
    assert result.data is None
    assert result.errors[0].extensions["code"] == "QUERY_COST_EXCEEDED"
    assert "exceeds budget 100" in result.errors[0].message
    assert resolved == []  # no resolver ran


def test_legitimate_query_within_budget_unaffected(env):
    result = schema.execute_sync(CHEAP, context_value=_ctx())
    assert result.errors is None
    assert result.data["node"]["id"] == "a"
    assert len(result.data["node"]["neighbours"]) == 2


def test_budget_scales_with_api_key_tier(env):
    policy.set_tier_limits({"enterprise": {"graphql_max_cost": 2000}})
    free = schema.execute_sync(NESTED, context_value=_ctx({"X-LedgerLens-Api-Key": _key("free")}))
    assert free.errors[0].extensions["budget"] == policy.get_tier_limits("free")["graphql_max_cost"]

    ent = schema.execute_sync(
        NESTED, context_value=_ctx({"X-LedgerLens-Api-Key": _key("enterprise")})
    )
    assert ent.errors is None


def test_admin_tier_is_unlimited(env):
    result = schema.execute_sync(
        NESTED, context_value=_ctx({"X-LedgerLens-Admin-Key": "admin-secret"})
    )
    assert result.errors is None


def test_depth_limit_rejects_regardless_of_tier(env):
    deep = '{ node(id: "a") { ' + "neighbours { " * 9 + "id" + " }" * 9 + " } }"
    result = schema.execute_sync(deep, context_value=_ctx({"X-LedgerLens-Admin-Key": "admin-secret"}))
    assert result.errors[0].extensions["code"] == "QUERY_TOO_DEEP"
    assert resolved == []


def test_production_schema_has_cost_limiter():
    pytest.importorskip("pandas")
    from api.graphql_schema import schema as prod_schema

    assert QueryCostLimiter in prod_schema.extensions
