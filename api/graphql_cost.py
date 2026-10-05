"""Static GraphQL query-cost analysis (#970).

Every query is costed from its AST before execution:

- each selected field costs 1;
- a list-returning field multiplies the cost of its sub-selection by
  ``list_multiplier`` (fan-out);
- nesting deeper than ``max_depth`` is rejected outright.

The budget comes from the caller's API-key tier (``graphql_max_cost`` in
:mod:`api.policy`), so GraphQL budgets stay consistent with the REST / gRPC
rate-limit tiers. Queries over budget are rejected with a clear error and
never reach a resolver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    get_nullable_type,
    is_list_type,
)
from strawberry.extensions import SchemaExtension

from api import policy

logger = logging.getLogger("ledgerlens.graphql.cost")

DEFAULT_LIST_MULTIPLIER = 10
DEFAULT_MAX_DEPTH = 8


@dataclass
class QueryCost:
    cost: int
    depth: int


def calculate_query_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    operation_name: str | None = None,
    list_multiplier: int = DEFAULT_LIST_MULTIPLIER,
) -> QueryCost:
    """Return the static cost and max depth of the selected operation."""
    fragments = {
        d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)
    }
    operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if operation_name:
        operations = [o for o in operations if o.name and o.name.value == operation_name]

    total = QueryCost(0, 0)
    for op in operations:
        root = schema.get_root_type(op.operation)
        if root is None:
            continue
        cost, depth = _selection_cost(op.selection_set, root, fragments, list_multiplier, 1, set())
        total.cost += cost
        total.depth = max(total.depth, depth)
    return total


def _selection_cost(
    selection_set: SelectionSetNode | None,
    parent: GraphQLObjectType,
    fragments: dict[str, FragmentDefinitionNode],
    multiplier: int,
    depth: int,
    visiting: set[str],
) -> tuple[int, int]:
    if selection_set is None:
        return 0, depth - 1
    cost, max_depth = 0, depth
    for sel in selection_set.selections:
        if isinstance(sel, FieldNode):
            name = sel.name.value
            if name.startswith("__"):
                continue  # introspection / __typename are free
            field_def = parent.fields.get(name)
            if field_def is None:
                continue  # unknown fields are rejected by standard validation
            child_type = get_named_type(field_def.type)
            is_list = is_list_type(get_nullable_type(field_def.type))
            child_cost, child_depth = (0, depth)
            if sel.selection_set is not None and isinstance(child_type, GraphQLObjectType):
                child_cost, child_depth = _selection_cost(
                    sel.selection_set, child_type, fragments, multiplier, depth + 1, visiting
                )
            cost += 1 + child_cost * (multiplier if is_list else 1)
            max_depth = max(max_depth, child_depth)
        elif isinstance(sel, (InlineFragmentNode, FragmentSpreadNode)):
            if isinstance(sel, FragmentSpreadNode):
                frag_name = sel.name.value
                frag = fragments.get(frag_name)
                if frag is None or frag_name in visiting:
                    continue
                sub, visiting = frag.selection_set, visiting | {frag_name}
            else:
                sub = sel.selection_set
            c, d = _selection_cost(sub, parent, fragments, multiplier, depth, visiting)
            cost += c
            max_depth = max(max_depth, d)
    return cost, max_depth


def _budget_for_context(context) -> int:
    """Resolve the caller's tier budget from the request's credentials."""
    request = context.get("request") if isinstance(context, dict) else None
    key_meta = None
    if request is not None:
        key_meta = policy.resolve_credentials(
            admin_key=request.headers.get("X-LedgerLens-Admin-Key", ""),
            api_key=request.headers.get("X-LedgerLens-Api-Key", ""),
        )
    return policy.get_tier_limits(policy.tier_for(key_meta)).get("graphql_max_cost", 0)


class QueryCostLimiter(SchemaExtension):
    """Reject queries whose static cost exceeds the caller's tier budget."""

    list_multiplier = DEFAULT_LIST_MULTIPLIER
    max_depth = DEFAULT_MAX_DEPTH

    def on_validate(self):
        # Must run before ``yield``: strawberry returns pre_execution_errors from
        # inside the validation phase, so errors set afterwards would be ignored.
        self._check_cost()
        yield

    def _check_cost(self) -> None:
        ec = self.execution_context
        if ec.graphql_document is None:
            return
        result = calculate_query_cost(
            ec.schema._schema, ec.graphql_document, ec.operation_name, self.list_multiplier
        )
        if result.depth > self.max_depth:
            ec.pre_execution_errors = [
                GraphQLError(
                    f"Query depth {result.depth} exceeds maximum allowed depth {self.max_depth}",
                    extensions={"code": "QUERY_TOO_DEEP"},
                )
            ]
            return
        budget = _budget_for_context(ec.context)
        if budget > 0 and result.cost > budget:
            logger.warning("GraphQL query rejected: cost=%d budget=%d", result.cost, budget)
            ec.pre_execution_errors = [
                GraphQLError(
                    f"Query cost {result.cost} exceeds budget {budget} for this API key tier",
                    extensions={"code": "QUERY_COST_EXCEEDED", "cost": result.cost, "budget": budget},
                )
            ]
