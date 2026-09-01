"""Local override for tests/scripts/.

tests/conftest.py defines an autouse `patch_signing_key` fixture that
imports `config.settings` (pydantic-settings, validated against the full
app's environment) for every test under tests/. scripts/check_vuln_waivers.py
is a standalone, dependency-free script (stdlib + pyyaml only) with no
relationship to that runtime config, so pulling in the whole app stack here
would test nothing and only add a brittle, irrelevant dependency. pytest
resolves autouse fixtures from the nearest conftest.py first, so redefining
the same fixture name here as a no-op shadows the parent one for exactly
this directory, without touching its behavior for the rest of the suite.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="function")
def patch_signing_key():
    yield
