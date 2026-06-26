"""Testcontainers-based fixtures for the full LedgerLens E2E stack.

Spins up real SQLite (file-based, no container needed) and optionally Redis
via Testcontainers. Provides a fully initialized API client, trained models,
and database for end-to-end testing.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Mark all tests in this directory as e2e
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def e2e_tmpdir():
    """Session-scoped temporary directory for all E2E artifacts."""
    d = tempfile.mkdtemp(prefix="ledgerlens_e2e_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def e2e_db_path(e2e_tmpdir):
    return os.path.join(e2e_tmpdir, "e2e_test.db")


@pytest.fixture(scope="session")
def e2e_model_dir(e2e_tmpdir):
    d = os.path.join(e2e_tmpdir, "models")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def e2e_settings(e2e_db_path, e2e_model_dir):
    """Patch settings for the E2E session."""
    from unittest.mock import patch
    from types import SimpleNamespace

    env_overrides = {
        "LEDGERLENS_DB_PATH": e2e_db_path,
        "MODEL_DIR": e2e_model_dir,
        "HORIZON_URL": "https://horizon-testnet.stellar.org",
        "HORIZON_STREAM_URL": "https://horizon-testnet.stellar.org",
        "REDIS_URL": "redis://localhost:6379/0",
        "LEDGERLENS_MODEL_SIGNING_KEY": "e2e-test-signing-key",
        "LEDGERLENS_ADMIN_API_KEY": "e2e-admin-key",
    }
    with patch.dict(os.environ, env_overrides):
        yield env_overrides


@pytest.fixture(scope="session")
def e2e_trained_models(e2e_model_dir, e2e_settings):
    """Train a minimal model set for E2E tests."""
    import numpy as np
    import joblib
    from sklearn.ensemble import RandomForestClassifier
    from xgboost import XGBClassifier
    from lightgbm import LGBMClassifier
    from detection.model_signing import sign_model_file

    np.random.seed(42)
    X = np.random.randn(100, 15)
    y = (X[:, 0] > 0).astype(int)

    signing_key = b"e2e-test-signing-key"

    models = {
        "random_forest": RandomForestClassifier(n_estimators=5, random_state=42).fit(X, y),
        "xgboost": XGBClassifier(n_estimators=5, eval_metric="logloss", random_state=42).fit(X, y),
        "lightgbm": LGBMClassifier(n_estimators=5, random_state=42, verbose=-1).fit(X, y),
    }

    for name, model in models.items():
        path = os.path.join(e2e_model_dir, f"{name}.joblib")
        joblib.dump(model, path)
        sign_model_file(path, signing_key)

    return models


@pytest.fixture(scope="session")
def e2e_db_initialized(e2e_db_path, e2e_settings):
    """Initialize the E2E database schema."""
    import sqlite3

    with sqlite3.connect(e2e_db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                asset_pair TEXT NOT NULL,
                score INTEGER NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 0,
                benford_flag INTEGER NOT NULL DEFAULT 0,
                ml_flag INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL,
                shap_json TEXT,
                disputed INTEGER NOT NULL DEFAULT 0,
                namespace_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS score_disputes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                asset_pair TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                wallet TEXT,
                asset_pair TEXT,
                score REAL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
    return e2e_db_path


@pytest.fixture
def e2e_client(e2e_db_initialized, e2e_trained_models, e2e_model_dir, e2e_db_path):
    """Provide a FastAPI TestClient wired to the E2E stack."""
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
            yield client
