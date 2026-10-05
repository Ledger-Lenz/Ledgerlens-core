"""ED25519 + HMAC-SHA256 model artifact signing and integrity verification.

Provides two signing mechanisms:
1. HMAC-SHA256 (legacy): symmetric key signing via sign_model_file / verify_model_file.
2. ED25519 (recommended): asymmetric key signing via ModelSigner class.
3. Sigstore / cosign keyless signing (Issue #934): transparency-log-backed provenance
   via :class:`SigstoreSigner`.  New models MUST be signed via this mechanism.
   :func:`verify_sigstore_attestation` is called by model_inference.py before
   loading any model artifact; loading is refused when no valid attestation exists.

Sigstore keyless signing overview
----------------------------------
Signing uses ``sigstore-python`` (``pip install sigstore``).  The signer
obtains a short-lived OIDC identity certificate from Sigstore's Fulcio CA and
records a transparency-log entry in Rekor, producing a ``<artifact>.sigstore``
bundle file alongside the model artifact.  Verification checks the bundle
against the Rekor log without requiring a pre-distributed key.

Migration path for existing signed artifacts
--------------------------------------------
Run ``python -m detection.model_signing migrate <model_dir>`` to re-sign all
existing HMAC/ED25519-signed models under ``model_dir`` with Sigstore.
See ``docs/model_signing_migration.md`` for step-by-step instructions.

safe_joblib_load is the only sanctioned deserialization path for model artifacts
under model_dir. No call site outside this module may call joblib.load directly
on a model_dir path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path

import joblib

logger = logging.getLogger("ledgerlens.model_signing")


class ModelIntegrityError(RuntimeError):
    """Raised when a model file is missing a signature, or its signature does not verify."""


# ---------------------------------------------------------------------------
# ED25519 asymmetric signing (recommended)
# ---------------------------------------------------------------------------


class ModelSigner:
    """Sign and verify model artifacts using ED25519 asymmetric keys."""

    SIG_SUFFIX = ".sig"

    def __init__(self, public_key_b64: str, private_key_b64: str | None = None):
        """
        public_key_b64: base64-encoded 32-byte ED25519 public key (from settings.py).
        private_key_b64: base64-encoded 32-byte private key (from env); required for signing.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        pub_bytes = base64.b64decode(public_key_b64)
        self._public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        self._private_key: Ed25519PrivateKey | None = None
        if private_key_b64:
            priv_bytes = base64.b64decode(private_key_b64)
            self._private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)

    def _digest(self, model_path: Path) -> bytes:
        """Compute SHA-256 of model file contents."""
        return hashlib.sha256(model_path.read_bytes()).digest()

    def sign(self, model_path: Path) -> Path:
        """Sign model artifact. Writes <model_path>.sig. Returns path to .sig file."""
        if self._private_key is None:
            raise RuntimeError("Private key not loaded; cannot sign.")
        digest = self._digest(model_path)
        signature = self._private_key.sign(digest)
        sig_path = model_path.with_suffix(model_path.suffix + self.SIG_SUFFIX)
        sig_path.write_bytes(base64.b64encode(signature))
        logger.info("Signed model: %s", model_path.name)
        return sig_path

    def verify(self, model_path: Path) -> None:
        """Verify model artifact integrity. Raises ModelIntegrityError on failure."""
        from cryptography.exceptions import InvalidSignature

        sig_path = model_path.with_suffix(model_path.suffix + self.SIG_SUFFIX)
        if not sig_path.exists():
            raise ModelIntegrityError(
                f"Missing signature file for model: {model_path.name}"
            )
        signature = base64.b64decode(sig_path.read_bytes())
        digest = self._digest(model_path)
        try:
            self._public_key.verify(signature, digest)
        except InvalidSignature:
            raise ModelIntegrityError(
                f"Model integrity check FAILED for {model_path.name}. "
                "The model file may have been tampered with."
            )
        logger.info("Verification OK: %s", model_path.name)


def get_model_signer(require_private_key: bool = False) -> ModelSigner:
    """Factory that builds a ModelSigner from settings / environment."""
    from config.settings import settings

    public_key = settings.model_signing_public_key
    if not public_key:
        raise ModelIntegrityError(
            "MODEL_SIGNING_PUBLIC_KEY is not configured in settings. "
            "Run `python cli.py generate-signing-key` to create a keypair."
        )
    private_key = os.environ.get("MODEL_SIGNING_PRIVATE_KEY", "") or None
    if require_private_key and not private_key:
        raise RuntimeError(
            "MODEL_SIGNING_PRIVATE_KEY environment variable is not set. "
            "Cannot sign models without a private key."
        )
    return ModelSigner(public_key, private_key)


# ---------------------------------------------------------------------------
# HMAC-SHA256 legacy signing (kept for backward compatibility)
# ---------------------------------------------------------------------------


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
    """Compute HMAC-SHA256 over the file's bytes and write it to ``{path}.sig``."""
    _require_key(signing_key)
    with open(path, "rb") as f:
        data = f.read()
    mac = hmac.new(signing_key, data, hashlib.sha256).hexdigest()
    with open(_sig_path(path), "w") as f:
        f.write(mac + "\n")


def verify_model_file(path: str, signing_key: bytes) -> None:
    """Raise ModelIntegrityError if ``{path}.sig`` is missing or does not match."""
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
    """Verify model file integrity then deserialize with joblib."""
    verify_model_file(path, signing_key)
    return joblib.load(path)


# ---------------------------------------------------------------------------
# Sigstore / cosign keyless signing (Issue #934)
# ---------------------------------------------------------------------------

_SIGSTORE_BUNDLE_SUFFIX = ".sigstore"

# Set to False in environments where Sigstore verification is not yet enforced
# (e.g. local dev with no OIDC provider).  In production this MUST be True.
SIGSTORE_ENFORCEMENT_ENABLED: bool = True


class SigstoreAttestationError(ModelIntegrityError):
    """Raised when a Sigstore/cosign attestation is missing or invalid."""


class SigstoreSigner:
    """Sign and verify model artifacts using Sigstore keyless signing.

    Signing requires the ``sigstore`` Python package and a valid OIDC identity
    (e.g. GitHub Actions OIDC token, Google Workload Identity).

    Produces a ``<artifact>.sigstore`` bundle file containing the certificate,
    signature, and Rekor transparency-log entry.  The bundle is the only
    artefact needed for offline verification; the private key is ephemeral.
    """

    @staticmethod
    def sign(model_path: Path) -> Path:
        """Sign *model_path* with Sigstore keyless signing.

        Returns the path to the ``.sigstore`` bundle file.

        Requires: ``pip install sigstore``
        """
        try:
            from sigstore.sign import SigningContext
            from sigstore.models import Bundle
        except ImportError as exc:
            raise ImportError(
                "sigstore package is required for Sigstore signing. "
                "Install it with: pip install sigstore"
            ) from exc

        bundle_path = model_path.with_suffix(model_path.suffix + _SIGSTORE_BUNDLE_SUFFIX)
        ctx = SigningContext.production()
        with ctx.signer() as signer:
            result = signer.sign_artifact(input_=model_path.read_bytes())
        bundle_path.write_text(result.to_bundle().to_json())
        logger.info("Sigstore signed model: %s -> %s", model_path.name, bundle_path.name)
        return bundle_path

    @staticmethod
    def verify(model_path: Path, expected_identity: str | None = None, expected_issuer: str | None = None) -> None:
        """Verify the Sigstore attestation for *model_path*.

        Raises :class:`SigstoreAttestationError` when:
        - The ``.sigstore`` bundle file is missing.
        - Bundle verification against the Rekor log fails.
        - The signer identity/issuer does not match *expected_identity* /
          *expected_issuer* when those are provided.

        Args:
            model_path: Path to the model artifact.
            expected_identity: Optional OIDC identity (email / sub claim) to
                enforce, e.g. ``"ci@my-org.iam.gserviceaccount.com"``.
            expected_issuer: Optional OIDC issuer URL to enforce, e.g.
                ``"https://accounts.google.com"``.
        """
        try:
            from sigstore.verify import Verifier
            from sigstore.verify.policy import Identity
            from sigstore.models import Bundle
        except ImportError as exc:
            raise ImportError(
                "sigstore package is required for Sigstore verification. "
                "Install it with: pip install sigstore"
            ) from exc

        bundle_path = model_path.with_suffix(model_path.suffix + _SIGSTORE_BUNDLE_SUFFIX)
        if not bundle_path.exists():
            raise SigstoreAttestationError(
                f"Missing Sigstore bundle for model: {model_path.name}. "
                "Re-sign the artifact with SigstoreSigner.sign() before use."
            )

        try:
            bundle_json = bundle_path.read_text()
            bundle = Bundle.from_json(bundle_json)
        except Exception as exc:
            raise SigstoreAttestationError(
                f"Could not parse Sigstore bundle for {model_path.name}: {exc}"
            ) from exc

        verifier = Verifier.production()
        policy = Identity(
            identity=expected_identity or "",
            issuer=expected_issuer or "",
        ) if (expected_identity or expected_issuer) else None

        try:
            if policy is not None:
                verifier.verify_artifact(
                    input_=model_path.read_bytes(),
                    bundle=bundle,
                    policy=policy,
                )
            else:
                verifier.verify_artifact(
                    input_=model_path.read_bytes(),
                    bundle=bundle,
                )
        except Exception as exc:
            raise SigstoreAttestationError(
                f"Sigstore attestation verification FAILED for {model_path.name}: {exc}. "
                "The model artifact may have been tampered with or signed by an "
                "unauthorised identity."
            ) from exc

        logger.info("Sigstore verification OK: %s", model_path.name)


def verify_sigstore_attestation(
    model_path: str,
    expected_identity: str | None = None,
    expected_issuer: str | None = None,
) -> None:
    """Convenience wrapper — verify Sigstore attestation for *model_path*.

    When ``SIGSTORE_ENFORCEMENT_ENABLED`` is ``False`` (dev/local mode), logs a
    warning and returns without raising so local workflows aren't blocked.

    Raises :class:`SigstoreAttestationError` in production when the attestation
    is missing or invalid.
    """
    if not SIGSTORE_ENFORCEMENT_ENABLED:
        logger.warning(
            "Sigstore enforcement is disabled (SIGSTORE_ENFORCEMENT_ENABLED=False). "
            "Skipping attestation check for %s. Do NOT use in production.",
            os.path.basename(model_path),
        )
        return

    SigstoreSigner.verify(
        Path(model_path),
        expected_identity=expected_identity,
        expected_issuer=expected_issuer,
    )
