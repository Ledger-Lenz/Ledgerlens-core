"""Regression tests for shared conftest.py fixtures.

These tests lock in the correctness of the autouse fixtures defined in
conftest.py so that future changes to the shared test infrastructure cannot
silently break the signing-key injection or the stellar_sdk isolation
mechanism.
"""

from __future__ import annotations

import sys

import pytest


# ---------------------------------------------------------------------------
# patch_signing_key — verify the yield-based teardown works correctly
# ---------------------------------------------------------------------------

TEST_KEY = "test-signing-key-for-unit-tests-only"


def test_patch_signing_key_injects_test_key_into_settings():
    """The autouse patch_signing_key fixture must make settings.ledgerlens_model_signing_key
    equal to TEST_SIGNING_KEY for the duration of every test body.

    This test would have FAILED before the yield was added to the fixture:
    monkeypatch teardown ran before the test body, so the setting was
    restored to its original value before any assertion could check it.
    """
    import config.settings as settings_module

    assert settings_module.settings.ledgerlens_model_signing_key == TEST_KEY, (
        "patch_signing_key fixture did not inject the test key — "
        "the fixture may be missing its yield statement"
    )


def test_patch_signing_key_injects_env_var():
    """LEDGERLENS_MODEL_SIGNING_KEY env var must be set to the test value."""
    import os

    assert os.environ.get("LEDGERLENS_MODEL_SIGNING_KEY") == TEST_KEY


# ---------------------------------------------------------------------------
# _test_file_name helper — portable across pytest versions
# ---------------------------------------------------------------------------

def test_test_file_name_returns_basename_only():
    """_test_file_name must return just the filename, not the full path."""
    from conftest import _test_file_name  # noqa: PLC0415

    class _FakeRequest:
        """Simulates pytest's request object with a path attribute."""
        class path:
            name = "test_conftest_fixtures.py"

    name = _test_file_name(_FakeRequest())
    assert name == "test_conftest_fixtures.py"
    assert "/" not in name
    assert "\\" not in name


def test_test_file_name_fallback_uses_pathlib():
    """_test_file_name fallback (fspath) must still return basename only."""
    from pathlib import Path

    from conftest import _test_file_name  # noqa: PLC0415

    class _FakeRequestLegacy:
        """Simulates a pre-pytest-7 request object with fspath but no path."""
        fspath = "/some/long/path/tests/test_foo.py"

    # Temporarily hide the 'path' attribute to force the fallback branch.
    request = _FakeRequestLegacy()
    name = _test_file_name(request)
    assert name == "test_foo.py"


# ---------------------------------------------------------------------------
# _REAL_STELLAR_SDK_TEST_FILES — isolation set is consistent
# ---------------------------------------------------------------------------

def test_real_stellar_sdk_set_contains_expected_files():
    from conftest import _REAL_STELLAR_SDK_TEST_FILES  # noqa: PLC0415

    expected = {
        "test_bridge_integrity.py",
        "test_bridge_loader.py",
        "test_cross_chain_linker.py",
        "test_cross_chain_features.py",
    }
    assert expected == _REAL_STELLAR_SDK_TEST_FILES


def test_real_stellar_sdk_set_is_frozenset():
    from conftest import _REAL_STELLAR_SDK_TEST_FILES  # noqa: PLC0415

    assert isinstance(_REAL_STELLAR_SDK_TEST_FILES, frozenset)
