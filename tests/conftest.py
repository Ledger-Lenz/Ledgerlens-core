"""Shared test fixtures."""

import pytest

TEST_SIGNING_KEY = "test-signing-key-for-unit-tests-only"


@pytest.fixture(autouse=True)
def patch_signing_key(monkeypatch):
    """Inject a test signing key into settings for every test."""
    import config.settings as settings_module

    monkeypatch.setenv("LEDGERLENS_MODEL_SIGNING_KEY", TEST_SIGNING_KEY)
    object.__setattr__(settings_module.settings, "model_signing_key", TEST_SIGNING_KEY)
