#!/usr/bin/env python3
"""Lint Prometheus metric names and labels against docs/metric_naming.md.

Statically scans Python sources for ``Counter(...)``, ``Gauge(...)``,
``Histogram(...)`` and ``Summary(...)`` constructor calls whose first argument
is a string literal, and checks the metric name and label names against the
project convention. Exits non-zero when any violation is found.

Usage::

    python scripts/lint_metric_names.py [PATH ...]   # default: repo root
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

METRIC_TYPES = {"Counter", "Gauge", "Histogram", "Summary"}
PREFIX = "ledgerlens_"
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
# Base units only (Prometheus best practice): seconds not ms, bytes not kb.
NON_BASE_UNITS = ("_ms", "_milliseconds", "_microseconds", "_minutes", "_hours", "_kb", "_mb", "_gb")
TIMING_UNITS = ("_seconds", "_bytes", "_ratio", "_score", "_blocks", "_ledgers")
RESERVED_LABELS = {"le", "quantile", "job", "instance"}
# Pre-convention names tracked in the migration plan (docs/metric_naming.md).
# Do not add new entries; rename the metric instead.
LEGACY_EXEMPT = {"ledgerlens_shadow_score_divergence"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "tests", "build", "dist", "target", "__pycache__"}


def check_metric(kind: str, name: str, labels: list[str]) -> list[str]:
    errors: list[str] = []
    if not name.startswith(PREFIX):
        errors.append(f"name must start with '{PREFIX}'")
    if not SNAKE_CASE.match(name):
        errors.append("name must be lower snake_case")
    if kind == "Counter" and not name.endswith("_total"):
        errors.append("counter names must end with '_total'")
    if kind != "Counter" and name.endswith("_total"):
        errors.append(f"only counters may end with '_total' ({kind.lower()})")
    if kind in {"Histogram", "Summary"} and not name.endswith(TIMING_UNITS):
        errors.append(f"{kind.lower()} names must end with a unit suffix {TIMING_UNITS}")
    stem = name[: -len("_total")] if name.endswith("_total") else name
    if stem.endswith(NON_BASE_UNITS):
        errors.append("use base units (_seconds, _bytes), not ms/minutes/kb")
    for label in labels:
        if not SNAKE_CASE.match(label):
            errors.append(f"label '{label}' must be lower snake_case")
        if label in RESERVED_LABELS:
            errors.append(f"label '{label}' is reserved by Prometheus")
    return errors


def _call_kind(node: ast.Call) -> str | None:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    return name if name in METRIC_TYPES else None


def _labels(node: ast.Call) -> list[str]:
    candidates = [kw.value for kw in node.keywords if kw.arg == "labelnames"]
    if len(node.args) >= 3:
        candidates.append(node.args[2])
    for value in candidates:
        if isinstance(value, (ast.List, ast.Tuple)):
            return [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def lint_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not (kind := _call_kind(node)):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            continue
        name = node.args[0].value
        if name in LEGACY_EXEMPT:
            continue
        for err in check_metric(kind, name, _labels(node)):
            problems.append(f"{path}:{node.lineno}: {name}: {err}")
    return problems


def iter_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*.py"):
            if not SKIP_DIRS.intersection(path.relative_to(root).parts):
                yield path


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path(__file__).resolve().parent.parent]
    problems = [p for f in iter_files(roots) for p in lint_file(f)]
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} metric naming violation(s). See docs/metric_naming.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
