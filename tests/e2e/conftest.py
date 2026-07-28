"""Fixtures for the LedgerLens E2E test suite.

Uses file-based SQLite (no external services required). Provides a fully
initialised FastAPI TestClient, trained models written to a temp directory,
and a session-scoped database with the correct schema.

Design notes
------------
* ``pytestmark`` is intentionally absent here — conftest.py is not a test
  module so module-level marks are silently ignored by pytest. Mark
  individual test modules with ``pytestmark = pytest.mark.e2e`` instead.
* ``REDIS_URL`` is not overridden — these tests exercise only the SQLite
  storage path. Injecting a Redis URL when Redis is not running creates
  confusing connection-refused noise in the test output and is actively
  misleading about the test's dependencies.
* The trained models use ``len(FEATURE_NAMES)`` features, not a hardcoded
  integer, so the fixture stays correct when new features are added to the
  feature-engineering pipeline.
* ``e2e_api_key`` provisions a ``read:scores``-scoped API key in the E2E
  database so that ``GET /v1/scores/{wallet}`` (which enforces
  ``require_scope("read:scores")``) returns 200 rather than 401.
* ``e2e_client`` exposes the provisioned API key via a ``headers`` attribute
  so individual tests can pass it without reimporting the store.
"""

import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope="session")
def e2e_tmpdir():
    """Session-scoped temporary directory for all E2E artefacts."""
    d = tempfile.mkdtemp(prefix="ledgerlens_e2e_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def e2e_db_path(e2e_tmpdir):
    """Absolute path to the session SQLite database file."""
    return os.path.join(e2e_tmpdir, "e2e_test.db")


@pytest.fixture(scope="session")
def e2e_model_dir(e2e_tmpdir):
    """Directory that holds the minimal trained models for E2E tests."""
    d = os.path.join(e2e_tmpdir, "models")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def e2e_settings(e2e_db_path, e2e_model_dir):
    """Patch environment variables for the duration of the E2E session.

    Only variables that are genuinely required for SQLite-only E2E tests are
    set here. ``REDIS_URL`` is intentionally omitted — injecting a Redis URL
    without a running Redis instance creates misleading connection errors and
    is not needed for these tests.
    """
    from unittest.mock import patch
    import config.settings as settings_module

    env_overrides = {
        "LEDGERLENS_DB_PATH": e2e_db_path,
        "MODEL_DIR": e2e_model_dir,
        "HORIZON_URL": "https://horizon-testnet.stellar.org",
        "HORIZON_STREAM_URL": "https://horizon-testnet.stellar.org",
        "LEDGERLENS_MODEL_SIGNING_KEY": "e2e-test-signing-key",
        "LEDGERLENS_ADMIN_API_KEY": "e2e-admin-key",
    }
    with patch.dict(os.environ, env_overrides):
        # Force the settings singleton to reflect the patched DB path so that
        # storage helpers (save_scores, create_api_key, …) that call
        # settings.db_path at runtime use the E2E file rather than the
        # default path.
        object.__setattr__(settings_module.settings, "db_path", e2e_db_path)
        object.__setattr__(settings_module.settings, "model_dir", e2e_model_dir)
        yield env_overrides
    # Restore original values after the session (best-effort; monkeypatch is
    # session-scoped so the process exits shortly after anyway).
    object.__setattr__(
        settings_module.settings,
        "db_path",
        settings_module.settings.model_fields["db_path"].default
        if "db_path" in settings_module.settings.model_fields
        else "ledgerlens.db",
    )


@pytest.fixture(scope="session")
def e2e_trained_models(e2e_model_dir, e2e_settings):
    """Train a minimal model set for E2E tests.

    The feature matrix is shaped ``(100, len(FEATURE_NAMES))`` so the models
    accept the same input dimensionality as the live inference path. Using a
    hardcoded column count here would silently misalign whenever FEATURE_NAMES
    is extended.
    """
    import numpy as np
    import joblib
    from sklearn.ensemble import RandomForestClassifier
    from xgboost import XGBClassifier
    from lightgbm import LGBMClassifier
    from detection.feature_engineering import FEATURE_NAMES
    from detection.model_signing import sign_model_file

    n_features = len(FEATURE_NAMES)
    np.random.seed(42)
    X = np.random.randn(100, n_features)
    y = (X[:, 0] > 0).astype(int)

    signing_key = b"e2e-test-signing-key"

    models = {
        "random_forest": RandomForestClassifier(
            n_estimators=5, random_state=42
        ).fit(X, y),
        "xgboost": XGBClassifier(
            n_estimators=5, eval_metric="logloss", random_state=42
        ).fit(X, y),
        "lightgbm": LGBMClassifier(
            n_estimators=5, random_state=42, verbose=-1
        ).fit(X, y),
    }

    for name, model in models.items():
        path = os.path.join(e2e_model_dir, f"{name}.joblib")
        joblib.dump(model, path)
        sign_model_file(path, signing_key)

    return models


@pytest.fixture(scope="session")
def e2e_db_initialized(e2e_db_path, e2e_settings):
    """Initialise the E2E database schema via the canonical storage module.

    Calling ``init_db`` rather than hand-rolling DDL keeps the schema in sync
    with any migrations added to ``detection/storage.py``.
    """
    from detection.storage import init_db

    init_db(e2e_db_path)
    return e2e_db_path


@pytest.fixture(scope="session")
def e2e_api_key(e2e_db_initialized, e2e_settings):
    """Provision a ``read:scores``-scoped API key in the E2E database.

    ``GET /v1/scores/{wallet}`` is protected by
    ``Depends(require_scope("read:scores"))``. Without a valid key in the
    database the endpoint returns 401, causing test_ingest_score_retrieve to
    fail for the wrong reason.

    Returns the plaintext key string so callers can build auth headers.
    """
    from detection.api_key_store import create_api_key

    key_meta = create_api_key(scopes=["read:scores"])
    return key_meta["plaintext_key"]


@pytest.fixture
def e2e_client(e2e_db_initialized, e2e_trained_models, e2e_model_dir, e2e_db_path, e2e_api_key):
    """Provide a FastAPI TestClient wired to the E2E stack.

    The ``headers`` attribute on the returned client is pre-populated with the
    ``X-LedgerLens-Api-Key`` header so callers that need scoped endpoints can
    pass ``client.headers`` directly.
    """
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    env = {
        "LEDGERLENS_DB_PATH": e2e_db_path,
        "MODEL_DIR": e2e_model_dir,
        "LEDGERLENS_MODEL_SIGNING_KEY": "e2e-test-signing-key",
        "LEDGERLENS_ADMIN_API_KEY": "e2e-admin-key",
    }
    with patch.dict(os.environ, env):
        from api.main import app

        with TestClient(app) as client:
            # Attach the read:scores API key so individual tests can pass it
            # to scoped endpoints without re-importing the key store.
            client.headers = {
                **client.headers,
                "X-LedgerLens-Api-Key": e2e_api_key,
            }
            yield client
