"""Replay the fuzz regression corpus on every CI run.

``fuzz/regression/<harness>/*.bin`` holds deduplicated crash reproducers,
populated automatically by ``scripts/fuzz_triage.py`` from the nightly fuzz
campaign (``.github/workflows/nightly_fuzz.yml``). Each input is fed through
the Atheris-free mirror of its harness from ``test_fuzz_harness_smoke`` and
must not raise.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.test_fuzz_harness_smoke import (
    _call_asset_parser,
    _call_evm_parser,
    _call_orderbook_parser,
    _call_solana_parser,
    _call_trade_parser,
)

_FUZZ_DIR = Path(__file__).resolve().parent.parent / "fuzz"
_REGRESSION_DIR = _FUZZ_DIR / "regression"

_CALLERS = {
    "fuzz_trade_parser": _call_trade_parser,
    "fuzz_asset_parser": _call_asset_parser,
    "fuzz_orderbook_event_parser": _call_orderbook_parser,
    "fuzz_evm_rpc_parser": _call_evm_parser,
    "fuzz_solana_vaa_parser": _call_solana_parser,
}

# The EVM parsers need the optional ``chain`` extra (web3).
_OPTIONAL_DEPS = {"fuzz_evm_rpc_parser": "web3"}

_CASES = sorted(
    (harness, path)
    for harness in _CALLERS
    for path in (_REGRESSION_DIR / harness).glob("*.bin")
)


def test_every_harness_has_a_regression_corpus() -> None:
    harnesses = {p.stem for p in _FUZZ_DIR.glob("fuzz_*.py")}
    assert harnesses == set(_CALLERS), "register new fuzz targets in _CALLERS"
    for harness in harnesses:
        assert (_REGRESSION_DIR / harness).is_dir()


@pytest.mark.parametrize(
    ("harness", "path"), _CASES, ids=[f"{h}/{p.name}" for h, p in _CASES]
)
def test_regression_input_does_not_crash(harness: str, path: Path) -> None:
    dep = _OPTIONAL_DEPS.get(harness)
    if dep and importlib.util.find_spec(dep) is None:
        pytest.skip(f"{dep} not installed")
    _CALLERS[harness](path.read_bytes())  # must not raise
