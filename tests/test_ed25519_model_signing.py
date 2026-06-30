"""Tests for ED25519 model artifact signing and integrity verification."""

import base64
import os
from unittest.mock import patch

import joblib
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from sklearn.ensemble import RandomForestClassifier

from detection.model_signing import ModelIntegrityError, ModelSigner


def _generate_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_b64 = base64.b64encode(
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode()
    pub_b64 = base64.b64encode(
        pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    return pub_b64, priv_b64


PUB_B64, PRIV_B64 = _generate_keypair()


@pytest.fixture
def model_path(tmp_path):
    model = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
    path = tmp_path / "test_model.joblib"
    joblib.dump(model, path)
    return path


@pytest.fixture
def signer():
    return ModelSigner(PUB_B64, PRIV_B64)


@pytest.fixture
def verifier():
    return ModelSigner(PUB_B64)


class TestSignProducesSigFile:
    def test_sign_creates_sig_file(self, signer, model_path):
        sig_path = signer.sign(model_path)
        assert sig_path.exists()
        assert sig_path.suffix == ".sig"
        content = sig_path.read_bytes()
        decoded = base64.b64decode(content)
        assert len(decoded) == 64  # ED25519 signature is 64 bytes


class TestVerifyCorrectSignature:
    def test_sign_then_verify(self, signer, model_path):
        signer.sign(model_path)
        signer.verify(model_path)  # must not raise


class TestVerifyWrongFile:
    def test_verify_different_file_with_wrong_sig(self, signer, tmp_path):
        file_a = tmp_path / "a.joblib"
        file_b = tmp_path / "b.joblib"
        joblib.dump({"a": 1}, file_a)
        joblib.dump({"b": 2}, file_b)
        signer.sign(file_a)
        # Copy file_a's sig to file_b
        sig_a = file_a.with_suffix(".joblib.sig")
        sig_b = file_b.with_suffix(".joblib.sig")
        sig_b.write_bytes(sig_a.read_bytes())
        with pytest.raises(ModelIntegrityError, match="FAILED"):
            signer.verify(file_b)


class TestVerifyTamperedModel:
    def test_tampered_model_raises(self, signer, model_path):
        signer.sign(model_path)
        with open(model_path, "ab") as f:
            f.write(b"\x00")
        with pytest.raises(ModelIntegrityError, match="FAILED"):
            signer.verify(model_path)


class TestVerifyMissingSigFile:
    def test_missing_sig_raises(self, signer, model_path):
        with pytest.raises(ModelIntegrityError, match="Missing signature"):
            signer.verify(model_path)


class TestSignWithoutPrivateKey:
    def test_sign_without_private_key_raises(self, verifier, model_path):
        with pytest.raises(RuntimeError, match="Private key not loaded"):
            verifier.sign(model_path)


class TestLoadModelPropagatesError:
    def test_verify_failure_prevents_load(self, signer, model_path):
        # No signature file → verify should raise before any load
        with pytest.raises(ModelIntegrityError):
            signer.verify(model_path)


class TestGenerateSigningKeyCLI:
    def test_generate_signing_key_output(self):
        from typer.testing import CliRunner
        from cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["generate-signing-key"])
        assert result.exit_code == 0
        assert "Public key" in result.output
        assert "Private key" in result.output
        lines = [ln.strip() for ln in result.output.strip().split("\n") if ln.strip()]
        b64_lines = [ln for ln in lines if len(ln) >= 44 and not ln.startswith(("Public", "Private", "WARNING"))]
        assert len(b64_lines) >= 2


class TestVerifyModelsCLI:
    def test_verify_models_all_valid(self, tmp_path):
        from typer.testing import CliRunner
        from cli import app

        pub_b64, priv_b64 = _generate_keypair()
        signer = ModelSigner(pub_b64, priv_b64)
        model = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
        path = tmp_path / "rf.joblib"
        joblib.dump(model, path)
        signer.sign(path)

        runner = CliRunner()
        with patch.dict(os.environ, {"MODEL_SIGNING_PRIVATE_KEY": priv_b64}):
            with patch("config.settings.settings") as mock_settings:
                mock_settings.model_dir = str(tmp_path)
                mock_settings.model_signing_public_key = pub_b64
                result = runner.invoke(app, ["verify-models", "--model-dir", str(tmp_path)])
        assert "OK" in result.output

    def test_verify_models_tampered_exits_nonzero(self, tmp_path):
        from typer.testing import CliRunner
        from cli import app

        pub_b64, priv_b64 = _generate_keypair()
        signer = ModelSigner(pub_b64, priv_b64)
        model = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
        path = tmp_path / "rf.joblib"
        joblib.dump(model, path)
        signer.sign(path)
        with open(path, "ab") as f:
            f.write(b"\xff")

        runner = CliRunner()
        with patch.dict(os.environ, {"MODEL_SIGNING_PRIVATE_KEY": priv_b64}):
            with patch("config.settings.settings") as mock_settings:
                mock_settings.model_dir = str(tmp_path)
                mock_settings.model_signing_public_key = pub_b64
                result = runner.invoke(app, ["verify-models", "--model-dir", str(tmp_path)])
        assert result.exit_code == 1


class TestFullTrainSignLoadCycle:
    def test_sign_and_load_roundtrip(self, signer, tmp_path):
        model = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
        path = tmp_path / "ensemble.joblib"
        joblib.dump(model, path)
        signer.sign(path)
        signer.verify(path)
        loaded = joblib.load(path)
        assert hasattr(loaded, "predict")
