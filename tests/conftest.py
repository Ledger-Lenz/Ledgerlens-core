"""Shared pytest fixtures and configuration.

Handles module-isolation concerns so that tests which mock ``stellar_sdk``
at collection time (``test_pipeline.py``, ``test_soroban_publisher.py``)
do not break tests that need the real SDK
(``test_bridge_loader.py``, ``test_cross_chain_*.py``).
"""

from __future__ import annotations

import sys

import pytest

TEST_SIGNING_KEY = "test-signing-key-for-unit-tests-only"


@pytest.fixture(autouse=True)
def patch_signing_key(monkeypatch):
    """Inject a test signing key into settings for every test."""
    import config.settings as settings_module

    monkeypatch.setenv("LEDGERLENS_MODEL_SIGNING_KEY", TEST_SIGNING_KEY)
    object.__setattr__(settings_module.settings, "ledgerlens_model_signing_key", TEST_SIGNING_KEY)


# Files that need the real stellar_sdk during test execution.
_REAL_STELLAR_SDK_TEST_FILES = frozenset([
    "test_bridge_loader.py",
    "test_cross_chain_linker.py",
    "test_cross_chain_features.py",
])


@pytest.fixture(autouse=True)
def _stellar_sdk_isolation(request):
    """Restore the real stellar_sdk for bridge/cross-chain tests.

    Some test modules replace ``sys.modules["stellar_sdk"]`` with a
    ``MagicMock`` at collection time.  Because bridge and cross-chain tests
    need the real SDK, this autouse fixture temporarily clears the mocked
    entries, imports the real package from disk, then restores the original
    state afterwards so that soroban/pipeline tests still see their mocks.
    """
    test_file = request.path.name if hasattr(request, "path") else str(request.fspath).split("/")[-1]
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
