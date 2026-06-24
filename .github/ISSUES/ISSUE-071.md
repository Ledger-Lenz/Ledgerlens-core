---
title: "Implement ED25519 Model Signing and Load-Time Integrity Verification for Ensemble Models"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/model_signing.py` so every trained model artifact (`.joblib` file) is signed with an ED25519 key at training time, and the signature is verified at inference load time. Reject unsigned or tampered models with a hard error that prevents inference from running on a compromised artifact. Store the public key in `config/settings.py`; the private key is loaded from an environment variable only and never written to disk. This prevents supply-chain attacks where a malicious model artifact is placed in the `models/` directory.

## Background & Context

LedgerLens's ensemble models (Random Forest, XGBoost, LightGBM) are serialised as `.joblib` files in the `models/` directory. Currently there is no integrity check on these files: if an attacker gains write access to the filesystem (e.g., via a compromised CI pipeline, container escape, or misconfigured volume mount), they could replace a `.joblib` file with a malicious serialised object. When Python's `joblib.load()` is called, arbitrary code can be executed during deserialisation (the `__reduce__` protocol).

ED25519 model signing provides two protections:
1. **Integrity**: a valid signature proves the file has not been modified since training.
2. **Authenticity**: a valid signature proves the file was produced by the LedgerLens training pipeline (which holds the private key), not by an external actor.

The signing scheme:
- At training time: compute `SHA-256(model_bytes)` and sign the digest with the ED25519 private key; write the signature to `<model_name>.sig` alongside the `.joblib` file.
- At load time: re-compute `SHA-256(model_bytes)`, load the `.sig` file, verify the signature against the public key from `settings.py`. If verification fails, raise `ModelIntegrityError` and abort.

The public key is embedded in `config/settings.py` as a base64-encoded string constant. The private key is in `MODEL_SIGNING_PRIVATE_KEY` environment variable (hex or base64 encoded). This means the public key can be audited in source control without revealing the private key.

## Objectives

- [ ] Implement `ModelSigner` class in `detection/model_signing.py` with `sign(model_path) -> bytes` and `verify(model_path) -> None`.
- [ ] `sign(model_path)` computes `SHA-256(file_bytes)`, signs with ED25519 private key, writes `<model_path>.sig` containing base64-encoded signature.
- [ ] `verify(model_path)` reads `.sig` file, recomputes SHA-256 of model file, verifies signature against public key from settings. Raises `ModelIntegrityError` (custom exception) on failure.
- [ ] `ModelIntegrityError` must be non-catchable-and-suppressible by callers: it should cause process exit if raised in the inference code path (`detection/model_inference.py`).
- [ ] Extend `detection/model_training.py` to call `ModelSigner.sign()` on every model artifact immediately after `joblib.dump()`.
- [ ] Extend `detection/model_inference.py` to call `ModelSigner.verify()` before every `joblib.load()` call.
- [ ] Add `cli.py verify-models` command that verifies all model artifacts in `MODEL_DIR` and exits non-zero if any fail.
- [ ] Store the public key as `MODEL_SIGNING_PUBLIC_KEY` in `config/settings.py` (base64-encoded raw bytes); private key in `MODEL_SIGNING_PRIVATE_KEY` environment variable only.
- [ ] Implement `cli.py generate-signing-key` command that generates a new ED25519 keypair, prints the public key for embedding in `settings.py`, and prints the private key for storing in the environment (one-time setup).
- [ ] All code paths covered by tests; ≥90% branch coverage.

## Technical Requirements

### `ModelSigner` (`detection/model_signing.py`)

```python
import hashlib, base64, os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)
from cryptography.exceptions import InvalidSignature

class ModelIntegrityError(RuntimeError):
    """Raised when a model file fails ED25519 signature verification."""

class ModelSigner:
    SIG_SUFFIX = ".sig"

    def __init__(self, public_key_b64: str, private_key_b64: Optional[str] = None):
        """
        public_key_b64: base64-encoded 32-byte ED25519 public key (from settings.py).
        private_key_b64: base64-encoded 32-byte private key (from env); required for signing.
        """
        pub_bytes = base64.b64decode(public_key_b64)
        self._public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        self._private_key: Optional[Ed25519PrivateKey] = None
        if private_key_b64:
            priv_bytes = base64.b64decode(private_key_b64)
            self._private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)

    def _digest(self, model_path: Path) -> bytes:
        """Compute SHA-256 of model file contents."""
        return hashlib.sha256(model_path.read_bytes()).digest()

    def sign(self, model_path: Path) -> Path:
        """
        Sign model artifact. Writes <model_path>.sig.
        Raises RuntimeError if private key not loaded.
        Returns path to .sig file.
        """
        if self._private_key is None:
            raise RuntimeError("Private key not loaded; cannot sign.")
        digest = self._digest(model_path)
        signature = self._private_key.sign(digest)
        sig_path = model_path.with_suffix(model_path.suffix + self.SIG_SUFFIX)
        sig_path.write_bytes(base64.b64encode(signature))
        return sig_path

    def verify(self, model_path: Path) -> None:
        """
        Verify model artifact integrity.
        Raises ModelIntegrityError if .sig missing or signature invalid.
        Never swallowed by the caller — this is a hard security boundary.
        """
        sig_path = model_path.with_suffix(model_path.suffix + self.SIG_SUFFIX)
        if not sig_path.exists():
            raise ModelIntegrityError(f"Missing signature file for model: {model_path.name}")
        signature = base64.b64decode(sig_path.read_bytes())
        digest = self._digest(model_path)
        try:
            self._public_key.verify(signature, digest)
        except InvalidSignature:
            raise ModelIntegrityError(
                f"Model integrity check FAILED for {model_path.name}. "
                "The model file may have been tampered with."
            )
```

### Integration in `model_inference.py`

```python
def load_model(model_name: str, model_dir: Path, signer: ModelSigner):
    """
    Load a signed model artifact.
    Verifies signature before loading. Any ModelIntegrityError propagates up
    and must not be caught in this function.
    """
    model_path = model_dir / f"{model_name}.joblib"
    signer.verify(model_path)    # Hard boundary: raises ModelIntegrityError on failure
    return joblib.load(model_path)
```

### Integration in `model_training.py`

```python
def save_and_sign_model(model, name: str, model_dir: Path, signer: ModelSigner) -> Path:
    model_path = model_dir / f"{name}.joblib"
    joblib.dump(model, model_path)
    sig_path = signer.sign(model_path)
    logger.info("Model signed: %s -> %s", model_path.name, sig_path.name)
    return model_path
```

### CLI commands

```python
@app.command("generate-signing-key")
def generate_signing_key():
    """
    Generate a new ED25519 keypair for model signing.
    Prints public key (for settings.py) and private key (for environment).
    ONLY run this during initial setup or key rotation.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_b64 = base64.b64encode(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    pub_b64 = base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    typer.echo(f"Public key (embed in config/settings.py as MODEL_SIGNING_PUBLIC_KEY):\n{pub_b64}")
    typer.echo(f"\nPrivate key (set as MODEL_SIGNING_PRIVATE_KEY env variable):\n{priv_b64}")
    typer.echo("\nWARNING: Store the private key securely. It cannot be recovered.")

@app.command("verify-models")
def verify_models():
    """Verify all model artifacts in MODEL_DIR. Exits non-zero if any fail."""
    signer = get_model_signer()
    failures = []
    for model_file in Path(settings.MODEL_DIR).glob("*.joblib"):
        try:
            signer.verify(model_file)
            typer.echo(f"OK: {model_file.name}")
        except ModelIntegrityError as e:
            typer.echo(f"FAIL: {e}", err=True)
            failures.append(model_file.name)
    if failures:
        raise typer.Exit(code=1)
```

### `config/settings.py` additions

```python
# ED25519 public key for model artifact signing (base64-encoded 32 bytes).
# Generate with: python cli.py generate-signing-key
MODEL_SIGNING_PUBLIC_KEY: str = os.getenv("MODEL_SIGNING_PUBLIC_KEY", "")
```

Private key is never in `settings.py`:
```
# .env.example
MODEL_SIGNING_PRIVATE_KEY=<base64-encoded 32-byte ED25519 private key from generate-signing-key>
```

## Security Considerations

- **Private key in environment only**: `MODEL_SIGNING_PRIVATE_KEY` must never be written to any file in the repository. The `generate-signing-key` CLI command outputs it to stdout only; the operator is responsible for storing it in their secret manager.
- **`ModelIntegrityError` must not be suppressible**: `model_inference.py` must not wrap `signer.verify()` in a bare `except Exception`. If the signature check fails, the process must abort inference. Add a `# noqa: no-bare-except` annotation ban in the linting configuration.
- **`.sig` files must be committed alongside `.joblib` files** (or generated in CI after training). If `.sig` files are absent from the `models/` directory, `verify-models` fails loudly. Add a CI step that runs `python cli.py verify-models` before any scoring step.
- **Public key in `settings.py` is not secret**, but changes to it must go through code review (it effectively trusts a new signer). Document this in `docs/model_signing.md`.
- **Key rotation**: document the rotation procedure: generate new keypair → retrain all models with new private key → update `MODEL_SIGNING_PUBLIC_KEY` in settings → commit → deploy. The old private key is then retired.
- **Do not log the private key or signature bytes**: log only `model_file.name` and `"verification OK"` or `"verification FAILED"` — never the raw bytes.

## Testing Requirements

- **Unit — `sign()` produces `.sig` file**: assert `<model>.joblib.sig` exists after sign; assert it is valid base64.
- **Unit — `verify()` correct signature**: sign a model; verify same model → no exception.
- **Unit — `verify()` wrong file**: sign file A; attempt to verify file B with A's sig → `ModelIntegrityError`.
- **Unit — `verify()` tampered model**: sign model; append one byte to model file; verify → `ModelIntegrityError`.
- **Unit — `verify()` missing sig file**: model without `.sig` → `ModelIntegrityError`.
- **Unit — `sign()` without private key**: construct `ModelSigner` with only public key; call `sign()` → `RuntimeError`.
- **Unit — `load_model` propagates error**: mock `signer.verify` raising `ModelIntegrityError`; assert `joblib.load` is never called.
- **CLI — `verify-models` exit codes**: all models valid → exit 0; one model tampered → exit 1.
- **CLI — `generate-signing-key` output format**: assert stdout contains two base64 strings each ≥44 characters (32 bytes base64-encoded).
- **Integration — full train-sign-load cycle**: train a model, sign it, load it via `load_model` → no exception.

## Documentation Requirements

- Docstrings on `ModelSigner.sign()`, `verify()`, and `ModelIntegrityError`.
- New file `docs/model_signing.md` covering: threat model, key management, rotation procedure, CI integration, and FAQ.
- Update `README.md` Features and Testing sections to mention model signing.
- Document `MODEL_SIGNING_PUBLIC_KEY` and `MODEL_SIGNING_PRIVATE_KEY` in `.env.example` and `config/settings.py`.
- Add `verify-models` and `generate-signing-key` to the CLI Reference table in README.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `ModelSigner.sign()` and `verify()` implemented; `ModelIntegrityError` defined.
- [ ] `model_training.py` signs every artifact via `save_and_sign_model()`.
- [ ] `model_inference.py` verifies every artifact before loading.
- [ ] `cli.py generate-signing-key` and `verify-models` commands operational.
- [ ] `MODEL_SIGNING_PUBLIC_KEY` in `settings.py`; private key in env only.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] CI step added: `python cli.py verify-models` runs before scoring.
- [ ] `docs/model_signing.md` written with key rotation procedure.
- [ ] `README.md`, `.env.example`, and `CHANGELOG.md` updated.
- [ ] No private key or signature bytes appear in logs.

## For Contributors

**Ideal contributor profile**: You have experience with software supply-chain security, specifically code-signing, model artifact integrity, or similar public-key integrity schemes. You understand the `cryptography` library's ED25519 implementation and can reason about the security properties of SHA-256 pre-image resistance in the context of signing model hashes. Familiarity with the ML model serialisation risks of `joblib`/`pickle` (arbitrary code execution on load) is essential for understanding why this issue exists.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., supply-chain security, Python cryptography, ML security, DevSecOps).
2. **Relevant experience**: model signing, artifact integrity, or software supply-chain security work you have shipped.
3. **Approach / thoughts**: would you sign the SHA-256 digest or the raw file bytes directly? What is your view on the tradeoff between signing at training time vs. signing in CI?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
