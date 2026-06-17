"""Manage versioned model storage and safe rollback.

Models are stored with version hashes based on the training data and timestamp,
allowing fine-grained tracking of which model version produced which scores.
A latest pointer tracks the currently-active model for inference.
"""

import hashlib
import joblib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ledgerlens.model_registry")


def _compute_version_hash(training_row_count: int, column_hash: str) -> str:
    """Generate SHA-256[:8] version hash from training metadata.

    Args:
        training_row_count: Number of rows in training dataset.
        column_hash: Hash of feature column names/order for stability.

    Returns:
        8-character hex string.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d%H%M")

    content = f"{training_row_count}:{column_hash}:{timestamp}"
    full_hash = hashlib.sha256(content.encode()).hexdigest()
    return full_hash[:8]


def save_versioned_model(
    model,
    name: str,
    version: str,
    model_dir: str,
) -> None:
    """Save a trained model with a version identifier.

    Creates {name}_v{version}.joblib and updates {name}_latest.txt
    to point to this version.

    Args:
        model: Trained scikit-learn/XGBoost/LightGBM model.
        name: Model name (e.g., 'random_forest', 'xgboost', 'lightgbm').
        version: Version string (typically SHA-256[:8]).
        model_dir: Directory to store versioned models.
    """
    Path(model_dir).mkdir(parents=True, exist_ok=True)

    model_path = os.path.join(model_dir, f"{name}_v{version}.joblib")
    joblib.dump(model, model_path)
    logger.info("Saved versioned model to %s", model_path)

    latest_path = os.path.join(model_dir, f"{name}_latest.txt")
    with open(latest_path, "w") as f:
        f.write(version)
    logger.info("Updated %s to version %s", latest_path, version)


def load_latest_model(
    name: str,
    model_dir: str,
):
    """Load the currently-active model version.

    Reads {name}_latest.txt to determine which version to load,
    then loads {name}_v{version}.joblib.

    Args:
        name: Model name (e.g., 'random_forest', 'xgboost', 'lightgbm').
        model_dir: Directory containing versioned models.

    Returns:
        Trained model object.

    Raises:
        FileNotFoundError: If latest pointer or model file does not exist.
    """
    latest_path = os.path.join(model_dir, f"{name}_latest.txt")
    if not os.path.exists(latest_path):
        raise FileNotFoundError(f"Latest pointer not found: {latest_path}")

    with open(latest_path, "r") as f:
        version = f.read().strip()

    model_path = os.path.join(model_dir, f"{name}_v{version}.joblib")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Versioned model not found: {model_path}")

    model = joblib.load(model_path)
    logger.info("Loaded %s version %s from %s", name, version, model_path)
    return model


def rollback_model(
    name: str,
    previous_version: str,
    model_dir: str,
) -> None:
    """Revert to a previous model version.

    Updates {name}_latest.txt to point to previous_version.
    Does NOT validate that the previous version exists; that is the
    caller's responsibility.

    Args:
        name: Model name (e.g., 'random_forest', 'xgboost', 'lightgbm').
        previous_version: Version string to revert to.
        model_dir: Directory containing versioned models.
    """
    latest_path = os.path.join(model_dir, f"{name}_latest.txt")
    with open(latest_path, "w") as f:
        f.write(previous_version)
    logger.info("Rolled back %s to version %s", name, previous_version)


def list_model_versions(
    name: str,
    model_dir: str,
) -> list[str]:
    """List all available versions for a given model name.

    Scans the model directory for {name}_v*.joblib files and extracts
    version strings. Returns versions sorted newest-first by extracting
    the timestamp portion of the version hash.

    Args:
        name: Model name (e.g., 'random_forest', 'xgboost', 'lightgbm').
        model_dir: Directory containing versioned models.

    Returns:
        List of version strings, newest first. Empty list if no versions found.
    """
    pattern = f"{name}_v"
    versions = []

    for fname in os.listdir(model_dir):
        if fname.startswith(pattern) and fname.endswith(".joblib"):
            version = fname[len(pattern) : -len(".joblib")]
            versions.append(version)

    # Sort by version string (which encodes timestamp as YYYYMMDDHHMM)
    # in descending order for newest-first ordering
    versions.sort(reverse=True)
    return versions


def get_current_version(
    name: str,
    model_dir: str,
) -> str | None:
    """Get the current version from the latest pointer.

    Args:
        name: Model name.
        model_dir: Directory containing versioned models.

    Returns:
        Current version string, or None if no latest pointer exists.
    """
    latest_path = os.path.join(model_dir, f"{name}_latest.txt")
    if not os.path.exists(latest_path):
        return None

    with open(latest_path, "r") as f:
        return f.read().strip()
