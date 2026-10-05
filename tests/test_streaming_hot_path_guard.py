"""Regression gate: the streaming-features hot path must stay free of blocking calls.

`detection.streaming_features` runs for every trade on the real-time SSE path,
so any synchronous I/O (DB, network, filesystem, subprocess, sleeps) added
there directly inflates end-to-end detection latency.  This static check fails
if such a dependency or call is (re)introduced; the latency benchmark in
`benchmarks/benchmark_streaming_features.py` guards the remaining CPU cost.
"""

import ast
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "detection" / "streaming_features.py"

FORBIDDEN_MODULES = {
    "sqlite3", "psycopg", "psycopg2", "sqlalchemy", "redis", "requests", "httpx",
    "aiohttp", "urllib", "http", "socket", "subprocess", "stellar_sdk", "web3",
    "detection.storage", "config.settings",
}
FORBIDDEN_CALLS = {"open", "input", "sleep", "urlopen", "connect", "read_csv", "read_sql", "read_parquet"}


def _module_tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_streaming_features_imports_no_blocking_io_modules():
    for name in _imported_modules(_module_tree()):
        root = name.split(".")[0]
        assert name not in FORBIDDEN_MODULES and root not in FORBIDDEN_MODULES, (
            f"streaming hot path must not import blocking-I/O module {name!r}"
        )


def test_streaming_features_makes_no_blocking_calls():
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            assert name not in FORBIDDEN_CALLS, (
                f"streaming hot path must not call blocking {name!r} (line {node.lineno})"
            )


@pytest.mark.benchmark
def test_streaming_update_latency_within_baseline():
    sys.path.insert(0, str(MODULE.parent.parent / "benchmarks"))
    import json

    import benchmark_streaming_features as bench

    baseline = json.loads(bench.BASELINE_PATH.read_text())
    assert bench.check_regression(bench.measure(5_000), baseline) == []
