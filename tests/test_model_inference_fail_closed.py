"""Model inference must fail closed on unsigned or tampered model artifacts (#1000)."""

import logging

import joblib
import pytest
from sklearn.linear_model import LogisticRegression

import config.settings as settings_module
from detection import model_inference
from detection.model_signing import ModelIntegrityError, sign_model_file


def _write_model(path, sign: bool):
    joblib.dump(LogisticRegression(), path)
    if sign:
        sign_model_file(str(path), settings_module.settings.model_signing_key.encode())


def test_unsigned_meta_learner_is_rejected(tmp_path, caplog):
    _write_model(tmp_path / model_inference._MODEL_FILENAMES["meta_learner"], sign=False)
    with caplog.at_level(logging.CRITICAL), pytest.raises(ModelIntegrityError):
        model_inference._load_models_base(str(tmp_path))
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_tampered_base_model_is_rejected(tmp_path, caplog):
    filename = next(
        f for n, f in model_inference._MODEL_FILENAMES.items() if n not in ("gnn", "meta_learner")
    )
    path = tmp_path / filename
    _write_model(path, sign=True)
    with open(path, "ab") as f:
        f.write(b"tampered")
    with caplog.at_level(logging.CRITICAL), pytest.raises(ModelIntegrityError):
        model_inference._load_models_base(str(tmp_path))
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
