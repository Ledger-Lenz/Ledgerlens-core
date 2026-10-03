#!/usr/bin/env python3
"""Enforce monitoring/cardinality_budget.yml against metric and rule definitions.

Scans Python sources for prometheus_client Counter/Gauge/Histogram/Summary
definitions and monitoring/*.yml for `by (...)` aggregations, and fails when:

- a label is on the forbidden list (unbounded values such as wallet addresses),
- a label has no declared bound in the budget file, or
- a metric's worst-case series count exceeds ``max_series_per_metric`` (or its
  justified entry in ``metric_overrides``).

Usage: python scripts/check_metric_cardinality.py [--root PATH]
Exit code 0 when within budget, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import math
import re
import sys
from pathlib import Path

import yaml

METRIC_TYPES = {"Counter", "Gauge", "Histogram", "Summary"}
# prometheus_client default histogram buckets (15) + +Inf, plus _sum and _count.
DEFAULT_HISTOGRAM_SERIES = 18
SKIP_DIRS = {"tests", "venv", ".venv", "node_modules", ".git", "build", "dist"}
BY_CLAUSE = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")


def _str_list(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [e.value for e in node.elts]
    return None


def find_metrics(root: Path) -> list[tuple[str, str, list[str], int]]:
    """Return (location, metric_name, labels, series_per_label_set) for each metric."""
    found = []
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in METRIC_TYPES or len(node.args) < 2:
                continue
            if not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args[:2]):
                continue  # e.g. collections.Counter
            kwargs = {k.arg: k.value for k in node.keywords}
            labels = _str_list(node.args[2] if len(node.args) > 2 else kwargs.get("labelnames")) or []
            per_set = 1
            if name == "Histogram":
                buckets = kwargs.get("buckets")
                # Explicit buckets: one series each, plus +Inf, _sum and _count.
                if isinstance(buckets, (ast.List, ast.Tuple)):
                    per_set = len(buckets.elts) + 3
                else:
                    per_set = DEFAULT_HISTOGRAM_SERIES
            loc = f"{path.relative_to(root)}:{node.lineno}"
            found.append((loc, node.args[0].value, labels, per_set))
    return found


def find_rule_labels(root: Path) -> list[tuple[str, list[str]]]:
    found = []
    for path in sorted((root / "monitoring").glob("*.yml")):
        if path.name == "cardinality_budget.yml":
            continue
        doc = yaml.safe_load(path.read_text()) or {}
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                for match in BY_CLAUSE.finditer(str(rule.get("expr", ""))):
                    labels = [lbl.strip() for lbl in match.group(1).split(",") if lbl.strip()]
                    name = rule.get("record") or rule.get("alert")
                    found.append((f"{path.relative_to(root)}:{name}", labels))
    return found


def check(root: Path, budget: dict) -> list[str]:
    bounds: dict[str, int] = budget.get("labels", {})
    forbidden = set(budget.get("forbidden_labels", []))
    limit = int(budget["max_series_per_metric"])
    overrides: dict[str, dict] = budget.get("metric_overrides") or {}
    errors = []
    for metric, override in overrides.items():
        if not str(override.get("justification", "")).strip():
            errors.append(f"metric_overrides.{metric}: justification is required")

    def label_errors(loc: str, labels: list[str]) -> list[str]:
        errs = []
        for label in labels:
            if label in forbidden:
                errs.append(f"{loc}: label {label!r} is forbidden (unbounded cardinality)")
            elif label not in bounds:
                errs.append(f"{loc}: label {label!r} has no bound in monitoring/cardinality_budget.yml")
        return errs

    for loc, metric, labels, per_set in find_metrics(root):
        errs = label_errors(f"{loc} ({metric})", labels)
        errors.extend(errs)
        if not errs:
            series = per_set * math.prod(bounds[label] for label in labels)
            metric_limit = int(overrides.get(metric, {}).get("max_series", limit))
            if series > metric_limit:
                errors.append(
                    f"{loc} ({metric}): worst-case {series} series exceeds budget of {metric_limit}"
                )
    for loc, labels in find_rule_labels(root):
        errors.extend(label_errors(loc, labels))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    budget = yaml.safe_load((args.root / "monitoring" / "cardinality_budget.yml").read_text())
    errors = check(args.root, budget)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} cardinality budget violation(s).", file=sys.stderr)
        return 1
    print("Metric cardinality within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
