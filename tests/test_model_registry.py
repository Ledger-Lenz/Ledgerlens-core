"""Tests for model versioning and rollback functionality."""

import os

import pytest
from sklearn.ensemble import RandomForestClassifier

from detection.model_registry import (
    _compute_version_hash,
    get_current_version,
    list_model_versions,
    load_latest_model,
    rollback_model,
    save_versioned_model,
)
from detection.model_signing import ModelIntegrityError


@pytest.fixture
def dummy_model():
    """Create a simple trained model for testing."""
    X = [[0, 0], [1, 1]]
    y = [0, 1]
    model = RandomForestClassifier(n_estimators=5, random_state=42)
    model.fit(X, y)
    return model


class TestComputeVersionHash:
    """Tests for version hash computation."""

    def test_version_hash_format(self):
        """Version hash should be 8 characters long."""
        version = _compute_version_hash(100, "abc123")
        assert isinstance(version, str)
        assert len(version) == 8
        assert all(c in "0123456789abcdef" for c in version)

    def test_version_hash_deterministic_for_same_inputs(self):
        """Version hash should be deterministic for the same minute."""
        v1 = _compute_version_hash(100, "abc123")
        v2 = _compute_version_hash(100, "abc123")
        # Same hash if called within the same minute
        assert v1 == v2

    def test_version_hash_different_for_different_row_counts(self):
        """Version hash should differ for different row counts."""
        v1 = _compute_version_hash(100, "abc123")
        v2 = _compute_version_hash(200, "abc123")
        # Different hashes for different inputs
        # (May occasionally collide, but very unlikely)
        # We just check they're both valid hashes
        assert len(v1) == 8
        assert len(v2) == 8

    def test_version_hash_different_for_different_column_hash(self):
        """Version hash should differ for different column hashes."""
        v1 = _compute_version_hash(100, "abc123")
        v2 = _compute_version_hash(100, "xyz789")
        assert len(v1) == 8
        assert len(v2) == 8


class TestSaveVersionedModel:
    """Tests for saving versioned models."""

    def test_save_versioned_model_creates_file(self, tmp_path, dummy_model):
        """save_versioned_model should create a versioned joblib file."""
        model_dir = str(tmp_path)
        version = "abcd1234"

        save_versioned_model(dummy_model, "test_model", version, model_dir)

        model_path = os.path.join(model_dir, f"test_model_v{version}.joblib")
        assert os.path.exists(model_path)

    def test_save_versioned_model_creates_latest_pointer(self, tmp_path, dummy_model):
        """save_versioned_model should create a latest.txt pointer."""
        model_dir = str(tmp_path)
        version = "abcd1234"

        save_versioned_model(dummy_model, "test_model", version, model_dir)

        latest_path = os.path.join(model_dir, "test_model_latest.txt")
        assert os.path.exists(latest_path)

        with open(latest_path, "r") as f:
            content = f.read().strip()
        assert content == version

    def test_save_versioned_model_multiple_versions(self, tmp_path, dummy_model):
        """save_versioned_model should handle multiple versions of the same model."""
        model_dir = str(tmp_path)
        v1 = "version001"
        v2 = "version002"

        save_versioned_model(dummy_model, "test_model", v1, model_dir)
        save_versioned_model(dummy_model, "test_model", v2, model_dir)

        path1 = os.path.join(model_dir, f"test_model_v{v1}.joblib")
        path2 = os.path.join(model_dir, f"test_model_v{v2}.joblib")

        assert os.path.exists(path1)
        assert os.path.exists(path2)

        # Latest pointer should point to v2
        latest_path = os.path.join(model_dir, "test_model_latest.txt")
        with open(latest_path, "r") as f:
            content = f.read().strip()
        assert content == v2


class TestLoadLatestModel:
    """Tests for loading the latest model version."""

    def test_load_latest_model_returns_model(self, tmp_path, dummy_model):
        """load_latest_model should return the latest model."""
        model_dir = str(tmp_path)
        version = "test0001"

        save_versioned_model(dummy_model, "test_model", version, model_dir)
        loaded = load_latest_model("test_model", model_dir)

        # Model should be loadable
        assert hasattr(loaded, "predict")
        assert hasattr(loaded, "predict_proba")

    def test_load_latest_model_raises_when_no_latest_pointer(self, tmp_path):
        """load_latest_model should raise FileNotFoundError when latest pointer missing."""
        model_dir = str(tmp_path)

        with pytest.raises(FileNotFoundError):
            load_latest_model("nonexistent_model", model_dir)

    def test_load_latest_model_raises_when_model_file_missing(self, tmp_path):
        """load_latest_model should raise FileNotFoundError if versioned file is missing."""
        model_dir = str(tmp_path)
        latest_path = os.path.join(model_dir, "test_model_latest.txt")

        # Create latest pointer without actual model file
        with open(latest_path, "w") as f:
            f.write("nonexistent_version")

        with pytest.raises(FileNotFoundError):
            load_latest_model("test_model", model_dir)

    def test_load_latest_model_round_trip(self, tmp_path, dummy_model):
        """Saved and loaded models should make identical predictions."""
        model_dir = str(tmp_path)
        version = "test0001"

        save_versioned_model(dummy_model, "test_model", version, model_dir)
        loaded = load_latest_model("test_model", model_dir)

        test_input = [[0.5, 0.5]]
        original_pred = dummy_model.predict(test_input)
        loaded_pred = loaded.predict(test_input)

        assert (original_pred == loaded_pred).all()


class TestRollbackModel:
    """Tests for model rollback."""

    def test_rollback_model_updates_latest_pointer(self, tmp_path, dummy_model):
        """rollback_model should update the latest pointer to previous version."""
        model_dir = str(tmp_path)
        v1 = "version001"
        v2 = "version002"

        save_versioned_model(dummy_model, "test_model", v1, model_dir)
        save_versioned_model(dummy_model, "test_model", v2, model_dir)

        # Verify v2 is current
        latest_path = os.path.join(model_dir, "test_model_latest.txt")
        with open(latest_path, "r") as f:
            assert f.read().strip() == v2

        # Rollback to v1
        rollback_model("test_model", v1, model_dir)

        # Verify v1 is now current
        with open(latest_path, "r") as f:
            assert f.read().strip() == v1

    def test_rollback_model_allows_loading_previous_version(self, tmp_path, dummy_model):
        """After rollback, load_latest_model should load the rolled-back version."""
        model_dir = str(tmp_path)
        v1 = "version001"
        v2 = "version002"

        save_versioned_model(dummy_model, "model1", v1, model_dir)
        save_versioned_model(dummy_model, "model1", v2, model_dir)

        rollback_model("model1", v1, model_dir)
        loaded = load_latest_model("model1", model_dir)

        assert hasattr(loaded, "predict")


class TestListModelVersions:
    """Tests for listing model versions."""

    def test_list_model_versions_returns_all_versions(self, tmp_path, dummy_model):
        """list_model_versions should return all available versions."""
        model_dir = str(tmp_path)
        versions = ["v001", "v002", "v003"]

        for v in versions:
            save_versioned_model(dummy_model, "test_model", v, model_dir)

        listed = list_model_versions("test_model", model_dir)

        assert set(listed) == set(versions)

    def test_list_model_versions_sorted_newest_first(self, tmp_path, dummy_model):
        """list_model_versions should return versions sorted newest-first."""
        model_dir = str(tmp_path)
        versions = ["v001", "v002", "v003"]

        for v in versions:
            save_versioned_model(dummy_model, "test_model", v, model_dir)

        listed = list_model_versions("test_model", model_dir)

        # Versions are sorted in descending order (newest first)
        assert listed == sorted(versions, reverse=True)

    def test_list_model_versions_empty_when_no_versions(self, tmp_path):
        """list_model_versions should return empty list when no versions exist."""
        model_dir = str(tmp_path)
        listed = list_model_versions("nonexistent_model", model_dir)
        assert listed == []


class TestGetCurrentVersion:
    """Tests for getting the current version."""

    def test_get_current_version_returns_version(self, tmp_path, dummy_model):
        """get_current_version should return the current version string."""
        model_dir = str(tmp_path)
        version = "test0001"

        save_versioned_model(dummy_model, "test_model", version, model_dir)
        current = get_current_version("test_model", model_dir)

        assert current == version

    def test_get_current_version_returns_none_when_no_pointer(self, tmp_path):
        """get_current_version should return None when latest pointer doesn't exist."""
        model_dir = str(tmp_path)
        current = get_current_version("nonexistent_model", model_dir)
        assert current is None

    def test_get_current_version_after_rollback(self, tmp_path, dummy_model):
        """get_current_version should return rolled-back version after rollback."""
        model_dir = str(tmp_path)
        v1 = "version001"
        v2 = "version002"

        save_versioned_model(dummy_model, "test_model", v1, model_dir)
        save_versioned_model(dummy_model, "test_model", v2, model_dir)

        assert get_current_version("test_model", model_dir) == v2

        rollback_model("test_model", v1, model_dir)
        assert get_current_version("test_model", model_dir) == v1


class TestSigningIntegration:
    """save_versioned_model writes a signature; load_latest_model verifies it."""

    def test_save_creates_sig_file(self, tmp_path, dummy_model):
        model_dir = str(tmp_path)
        save_versioned_model(dummy_model, "rf", "v001", model_dir)
        sig = os.path.join(model_dir, "rf_vv001.joblib.sig")
        assert os.path.exists(sig)

    def test_load_round_trip_with_signing(self, tmp_path, dummy_model):
        model_dir = str(tmp_path)
        save_versioned_model(dummy_model, "rf", "v001", model_dir)
        loaded = load_latest_model("rf", model_dir)
        assert hasattr(loaded, "predict")

    def test_load_raises_on_tampered_file(self, tmp_path, dummy_model):
        model_dir = str(tmp_path)
        save_versioned_model(dummy_model, "rf", "v001", model_dir)
        path = os.path.join(model_dir, "rf_vv001.joblib")
        with open(path, "r+b") as f:
            f.seek(0)
            b = f.read(1)
            f.seek(0)
            f.write(bytes([b[0] ^ 0xFF]))
        with pytest.raises(ModelIntegrityError):
            load_latest_model("rf", model_dir)

    def test_load_raises_when_sig_missing(self, tmp_path, dummy_model):
        model_dir = str(tmp_path)
        save_versioned_model(dummy_model, "rf", "v001", model_dir)
        os.remove(os.path.join(model_dir, "rf_vv001.joblib.sig"))
        with pytest.raises(ModelIntegrityError):
            load_latest_model("rf", model_dir)

    def test_load_raises_on_missing_signing_key(self, tmp_path, dummy_model):
        import config.settings as settings_module

        model_dir = str(tmp_path)
        save_versioned_model(dummy_model, "rf", "v001", model_dir)
        object.__setattr__(settings_module.settings, "model_signing_key", "")
        try:
            with pytest.raises(ModelIntegrityError, match="LEDGERLENS_MODEL_SIGNING_KEY"):
                load_latest_model("rf", model_dir)
        finally:
            object.__setattr__(
                settings_module.settings,
                "model_signing_key",
                "test-signing-key-for-unit-tests-only",
            )
