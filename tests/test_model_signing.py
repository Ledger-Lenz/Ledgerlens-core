"""Tests for HMAC-SHA256 model artifact signing and integrity verification."""

import os

import joblib
import pytest
from sklearn.ensemble import RandomForestClassifier

from detection.model_signing import (
    ModelIntegrityError,
    assert_within_model_dir,
    safe_joblib_load,
    sign_model_file,
    verify_model_file,
)

TEST_KEY = b"test-signing-key-for-unit-tests-only"


@pytest.fixture
def signed_model_file(tmp_path):
    """Write a simple model to disk and sign it."""
    model = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
    path = str(tmp_path / "model.joblib")
    joblib.dump(model, path)
    sign_model_file(path, TEST_KEY)
    return path


class TestSignAndVerifyRoundTrip:
    def test_sign_creates_sig_file(self, signed_model_file):
        assert os.path.exists(signed_model_file + ".sig")

    def test_verify_passes_on_valid_signature(self, signed_model_file):
        verify_model_file(signed_model_file, TEST_KEY)  # must not raise

    def test_safe_joblib_load_returns_model(self, signed_model_file):
        model = safe_joblib_load(signed_model_file, TEST_KEY)
        assert hasattr(model, "predict")

    def test_sign_overwrites_existing_sig(self, signed_model_file):
        sig_path = signed_model_file + ".sig"
        with open(sig_path, "w") as f:
            f.write("bad\n")
        sign_model_file(signed_model_file, TEST_KEY)
        verify_model_file(signed_model_file, TEST_KEY)  # must not raise after re-sign


class TestTamperDetection:
    def test_tampered_file_raises_model_integrity_error(self, signed_model_file):
        with open(signed_model_file, "r+b") as f:
            f.seek(0)
            original = f.read(1)
            f.seek(0)
            f.write(bytes([original[0] ^ 0xFF]))

        with pytest.raises(ModelIntegrityError):
            verify_model_file(signed_model_file, TEST_KEY)

    def test_tampered_file_safe_load_raises(self, signed_model_file):
        with open(signed_model_file, "r+b") as f:
            f.seek(0)
            original = f.read(1)
            f.seek(0)
            f.write(bytes([original[0] ^ 0xFF]))

        with pytest.raises(ModelIntegrityError):
            safe_joblib_load(signed_model_file, TEST_KEY)


class TestMissingSignature:
    def test_missing_sig_file_raises(self, tmp_path):
        path = str(tmp_path / "model.joblib")
        joblib.dump(object(), path)
        with pytest.raises(ModelIntegrityError, match="missing"):
            verify_model_file(path, TEST_KEY)

    def test_missing_sig_safe_load_raises(self, tmp_path):
        path = str(tmp_path / "model.joblib")
        joblib.dump(object(), path)
        with pytest.raises(ModelIntegrityError):
            safe_joblib_load(path, TEST_KEY)


class TestEmptyKey:
    def test_sign_raises_on_empty_key(self, tmp_path):
        path = str(tmp_path / "model.joblib")
        joblib.dump(object(), path)
        with pytest.raises(ModelIntegrityError, match="LEDGERLENS_MODEL_SIGNING_KEY"):
            sign_model_file(path, b"")

    def test_verify_raises_on_empty_key(self, signed_model_file):
        with pytest.raises(ModelIntegrityError, match="LEDGERLENS_MODEL_SIGNING_KEY"):
            verify_model_file(signed_model_file, b"")

    def test_safe_load_raises_on_empty_key(self, signed_model_file):
        with pytest.raises(ModelIntegrityError, match="LEDGERLENS_MODEL_SIGNING_KEY"):
            safe_joblib_load(signed_model_file, b"")


class TestWrongKey:
    def test_wrong_key_raises_model_integrity_error(self, signed_model_file):
        with pytest.raises(ModelIntegrityError):
            verify_model_file(signed_model_file, b"different-key")


class TestPathTraversal:
    def test_path_within_model_dir_is_allowed(self, tmp_path):
        path = str(tmp_path / "model.joblib")
        open(path, "w").close()
        assert_within_model_dir(path, str(tmp_path))  # must not raise

    def test_path_outside_model_dir_raises(self, tmp_path):
        subdir = tmp_path / "models"
        subdir.mkdir()
        outside = str(tmp_path / "evil.joblib")
        open(outside, "w").close()
        with pytest.raises(ModelIntegrityError, match="traversal"):
            assert_within_model_dir(outside, str(subdir))

    def test_path_traversal_via_dotdot_raises(self, tmp_path):
        subdir = tmp_path / "models"
        subdir.mkdir()
        traversal = str(subdir / ".." / "evil.joblib")
        open(str(tmp_path / "evil.joblib"), "w").close()
        with pytest.raises(ModelIntegrityError, match="traversal"):
            assert_within_model_dir(traversal, str(subdir))


class TestNoDirectJobLibLoadInCodebase:
    """Assert no .py file outside model_signing.py calls joblib.load directly."""

    def test_no_direct_joblib_load_outside_signing_module(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        violations = []
        for dirpath, _, filenames in os.walk(repo_root):
            if any(part.startswith(".") for part in dirpath.split(os.sep)):
                continue
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, repo_root)
                if rel in (
                    os.path.join("detection", "model_signing.py"),
                    os.path.join("tests", "test_model_signing.py"),
                ):
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if "joblib.load(" in line and not line.strip().startswith("#"):
                            violations.append(f"{rel}:{lineno}: {line.rstrip()}")
        assert violations == [], (
            "Direct joblib.load() calls found outside detection/model_signing.py:\n"
            + "\n".join(violations)
        )
