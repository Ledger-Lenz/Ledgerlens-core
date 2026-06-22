"""HMAC-SHA256 model artifact signing and integrity verification.

safe_joblib_load is the only sanctioned deserialization path for model artifacts
under model_dir. No call site outside this module may call joblib.load directly
on a model_dir path.
"""

import hashlib
import hmac
import os

import joblib


class ModelIntegrityError(RuntimeError):
    """Raised when a model file is missing a signature, or its signature does not verify."""


def _sig_path(path: str) -> str:
    return path + ".sig"


def _require_key(signing_key: bytes) -> None:
    if not signing_key:
        raise ModelIntegrityError(
            "LEDGERLENS_MODEL_SIGNING_KEY is not configured. "
            "Set this environment variable before loading or saving model artifacts."
        )


def assert_within_model_dir(path: str, model_dir: str) -> None:
    """Raise ModelIntegrityError if path resolves outside model_dir."""
    resolved_path = os.path.realpath(os.path.abspath(path))
    resolved_dir = os.path.realpath(os.path.abspath(model_dir))
    if not resolved_path.startswith(resolved_dir + os.sep):
        raise ModelIntegrityError(f"Path traversal detected for model artifact: {os.path.basename(path)!r}")


def sign_model_file(path: str, signing_key: bytes) -> None:
    """Compute HMAC-SHA256 over the file's bytes and write it to ``{path}.sig``.

    Overwrites any existing signature.
    """
    _require_key(signing_key)
    with open(path, "rb") as f:
        data = f.read()
    mac = hmac.new(signing_key, data, hashlib.sha256).hexdigest()
    with open(_sig_path(path), "w") as f:
        f.write(mac + "\n")


def verify_model_file(path: str, signing_key: bytes) -> None:
    """Raise ModelIntegrityError if ``{path}.sig`` is missing or does not match.

    Uses hmac.compare_digest for the comparison — never ``==``.
    Does not return a bool; callers cannot ignore a failed check.
    """
    _require_key(signing_key)
    sig_file = _sig_path(path)
    if not os.path.exists(sig_file):
        raise ModelIntegrityError(
            f"Signature file missing for model artifact: {os.path.basename(path)}"
        )
    try:
        with open(sig_file, "r") as f:
            stored_sig = f.read().strip()
    except OSError as exc:
        raise ModelIntegrityError(
            f"Cannot read signature for {os.path.basename(path)}: {exc}"
        ) from exc
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise ModelIntegrityError(
            f"Cannot read model file {os.path.basename(path)}: {exc}"
        ) from exc
    expected = hmac.new(signing_key, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, stored_sig):
        raise ModelIntegrityError(
            f"HMAC-SHA256 verification failed for model artifact: {os.path.basename(path)}"
        )


def safe_joblib_load(path: str, signing_key: bytes):
    """Verify model file integrity then deserialize with joblib.

    This is the only sanctioned deserialization path for model artifacts
    in this codebase. Do not call joblib.load directly on model_dir paths.
    """
    verify_model_file(path, signing_key)
    return joblib.load(path)
