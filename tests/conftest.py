"""Shared pytest fixtures and configuration.

Handles module-isolation concerns so that tests which mock ``stellar_sdk``
at collection time (``test_pipeline.py``, ``test_soroban_publisher.py``)
do not break tests that need the real SDK
(``test_bridge_loader.py``, ``test_cross_chain_*.py``).
"""

from __future__ import annotations

import os
import sys

import pytest

# MLflow ≥ 2.22 deprecated the filesystem tracking backend.
# Allow it in the test environment without requiring a database migration.
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
# Per-tier WAF rate limiting would throttle the shared "testclient" IP across
# the suite; tests that exercise it enable it explicitly.
os.environ.setdefault("LEDGERLENS_WAF_TIER_RATE_LIMIT_ENABLED", "false")

TEST_SIGNING_KEY = "test-signing-key-for-unit-tests-only"


@pytest.fixture(autouse=True, scope="function")
def patch_signing_key(monkeypatch):
    """Inject a test signing key into settings for every test.

    Uses ``monkeypatch.setattr`` on the property rather than
    ``object.__setattr__`` on the backing field so that pytest
    automatically restores the original value after each test.
    """
    import config.settings as settings_module

    monkeypatch.setenv("LEDGERLENS_MODEL_SIGNING_KEY", TEST_SIGNING_KEY)
    monkeypatch.setattr(settings_module.settings, "model_signing_key", TEST_SIGNING_KEY)


# Files that need the real stellar_sdk during test execution.
_REAL_STELLAR_SDK_TEST_FILES = frozenset([
    "test_bridge_integrity.py",
    "test_bridge_loader.py",
    "test_cross_chain_linker.py",
    "test_cross_chain_features.py",
])


def _test_file_name(request) -> str:
    """Return the bare filename (no directory) for the currently running test.

    Prefers ``request.path.name`` (pytest ≥ 7, a ``pathlib.Path``).  Falls
    back to deriving the name from ``request.fspath`` (a ``py.path.local``)
    for older pytest versions, using ``pathlib.Path`` to handle both POSIX
    and Windows separators correctly.
    """
    if hasattr(request, "path"):
        return request.path.name
    from pathlib import Path
    return Path(str(request.fspath)).name


@pytest.fixture(autouse=True)
def _stellar_sdk_isolation(request):
    """Restore the real stellar_sdk for bridge/cross-chain tests.

    Some test modules replace ``sys.modules["stellar_sdk"]`` with a
    ``MagicMock`` at collection time.  Because bridge and cross-chain tests
    need the real SDK, this autouse fixture temporarily clears the mocked
    entries, imports the real package from disk, then restores the original
    state afterwards so that soroban/pipeline tests still see their mocks.
    """
    test_file = _test_file_name(request)
    if test_file not in _REAL_STELLAR_SDK_TEST_FILES:
        yield
        return

    # Remove all stellar_sdk entries (may be MagicMocks) and save them.
    saved: dict[str, object] = {}
    for key in list(sys.modules):
        if key == "stellar_sdk" or key.startswith("stellar_sdk."):
            saved[key] = sys.modules.pop(key)

    # With sys.modules clear of stellar_sdk, Python will load the real package.
    import stellar_sdk  # noqa: F401

    yield

    # Remove whatever real stellar_sdk entries were loaded during the test.
    for key in list(sys.modules):
        if key == "stellar_sdk" or key.startswith("stellar_sdk."):
            del sys.modules[key]

    # Restore the saved (possibly mocked) state.
    sys.modules.update(saved)
