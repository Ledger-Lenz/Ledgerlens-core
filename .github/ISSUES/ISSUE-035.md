---
title: "Add Model Cryptographic Signing and Verification Pipeline"
labels: ["difficulty: advanced", "area: security", "type: feature"]
assignees: []
---

## Summary
LedgerLens model artifacts (`.joblib` files, `meta_learner.joblib`, `gnn_model.pt`) are loaded at inference time with no integrity verification. A compromised build environment, supply chain attack, or unauthorised filesystem write could replace a model file with a backdoored version that systematically under-scores specific wash-trading wallets without any alerting. This issue implements Ed25519 cryptographic signing of all model artifacts at training time and mandatory signature verification at inference load time, with hard rejection of any artifact whose signature does not verify — making model tampering detectable and non-silently-exploitable.

## Background & Context
`detection/model_registry.py` manages model versioning (version hash in filename, `latest.txt` pointer files, `training_metadata.json`). `detection/model_inference.py` loads models using `joblib.load(path)`, which deserializes Python objects from disk without any integrity check. This is a known attack surface:

1. **Pickle deserialization attacks**: `joblib` uses pickle internally; a tampered `.joblib` file containing a malicious `__reduce__` method would execute arbitrary code at `joblib.load()` time
2. **Silent weight replacement**: a `.joblib` file with valid pickle structure but modified model weights would load without error, producing subtly wrong predictions
3. **GNN weight injection**: `torch.load()` with `weights_only=False` (pre-PyTorch 2.0 default) is similarly exploitable

The mitigations:
1. **Ed25519 signing**: at training time, compute SHA-256 of the model file content and sign the hash with an Ed25519 private key; store the signature in a `.sig` sidecar file
2. **Verification at load**: before `joblib.load()` or `torch.load()`, verify the sidecar signature with the corresponding public key; raise `ModelIntegrityError` if verification fails or sidecar is absent
3. **Key management**: the signing private key lives only in the training environment (CI/CD secret); the public key is embedded in `config/settings.py` or committed to the repository

`detection/model_signing.py` is the planned location for this functionality.

## Objectives
- [ ] Implement `ModelSigner` class in `detection/model_signing.py` with `sign_artifact(model_path: Path) -> Path` (creates `.sig` sidecar) and `verify_artifact(model_path: Path) -> bool` (verifies `.sig`)
- [ ] Integrate `ModelSigner.sign_artifact()` into `detection/model_registry.py` `save_model()` so all model saves are automatically signed
- [ ] Integrate `ModelSigner.verify_artifact()` into `detection/model_inference.py` `ModelInference.__init__()` so all model loads are verified before deserialization; raise `ModelIntegrityError` on failure
- [ ] Add `cli.py verify-models` subcommand that verifies all current model artifacts and prints a per-file verification report

## Technical Requirements

**Ed25519 key pair generation (one-time setup):**
```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

private_key = Ed25519PrivateKey.generate()
public_key = private_key.public_key()

# Serialize for storage
private_bytes = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
public_bytes = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
```

Store:
- Private key: environment variable `LEDGERLENS_MODEL_SIGNING_KEY` (PEM string, training environment only; never commit)
- Public key: `config/model_signing_pubkey.pem` (committed to repository; used for verification)

**Signing procedure:**
```python
def sign_artifact(model_path: Path) -> Path:
    """Compute SHA-256 digest of model file, sign with Ed25519, write .sig sidecar."""
    with open(model_path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    private_key = load_private_key_from_env()  # reads LEDGERLENS_MODEL_SIGNING_KEY
    signature = private_key.sign(digest)  # 64-byte Ed25519 signature
    sig_path = model_path.with_suffix(model_path.suffix + ".sig")
    sig_data = {
        "algorithm": "ed25519",
        "digest_algorithm": "sha256",
        "file": model_path.name,
        "signature": base64.b64encode(signature).decode("ascii"),
        "signed_at": datetime.utcnow().isoformat() + "Z",
        "signer": "ledgerlens-training-pipeline",
    }
    with open(sig_path, "w") as f:
        json.dump(sig_data, f, indent=2)
    return sig_path
```

**Verification procedure:**
```python
def verify_artifact(model_path: Path) -> bool:
    """Verify .sig sidecar against model file content. Returns True if valid."""
    sig_path = model_path.with_suffix(model_path.suffix + ".sig")
    if not sig_path.exists():
        raise ModelIntegrityError(f"Signature file missing: {sig_path}")
    with open(sig_path) as f:
        sig_data = json.load(f)
    with open(model_path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    signature = base64.b64decode(sig_data["signature"])
    public_key = load_public_key_from_file()  # reads config/model_signing_pubkey.pem
    try:
        public_key.verify(signature, digest)  # raises InvalidSignature on failure
        return True
    except InvalidSignature:
        raise ModelIntegrityError(f"Signature verification FAILED for {model_path.name}")
```

**`ModelIntegrityError`:**
```python
class ModelIntegrityError(Exception):
    """Raised when a model artifact fails cryptographic verification."""
    pass
```

**`ModelInference.__init__()` integration:**
```python
for model_path in model_paths:
    ModelSigner.verify_artifact(model_path)  # raises ModelIntegrityError if tampered
    model = joblib.load(model_path)          # only reached if verification passed
```

**`verify-models` CLI command:**
```
$ python cli.py verify-models
Verifying model artifacts...
  random_forest_v12a3b4c5.joblib     ✓ VALID  (signed 2026-06-24T09:25:23Z)
  xgboost_v12a3b4c5.joblib           ✓ VALID
  lightgbm_v12a3b4c5.joblib          ✓ VALID
  meta_learner.joblib                 ✓ VALID
  gnn_model.pt                        ✗ MISSING SIGNATURE
All verified: 4/5. 1 artifact(s) require attention.
```

**Key rotation procedure:**
- When the signing key is rotated: generate new key pair, re-sign all existing model artifacts with the new key, update `config/model_signing_pubkey.pem`
- Document key rotation steps in `docs/model_signing.md`
- Add a `--re-sign` flag to `cli.py verify-models` that re-signs all artifacts (requires `LEDGERLENS_MODEL_SIGNING_KEY` to be set)

**Dependency:**
- `cryptography>=41.0.0` added to `requirements.txt` (likely already present as a transitive dependency of `stellar-sdk`; verify and pin)

**Sidecar file convention:**
- `.joblib` → `.joblib.sig`
- `.pt` → `.pt.sig`
- `.txt` (latest pointers) → not signed (content is just a version hash, not executable)
- `.json` (metadata) → optionally signed; required for `training_metadata.json`

## Security Considerations
- `LEDGERLENS_MODEL_SIGNING_KEY` must never be logged, committed to git, or included in error messages; when loading the key, immediately after parsing discard the raw PEM string (`del pem_string`)
- The public key file `config/model_signing_pubkey.pem` must be committed as a read-only file; a CI check should fail if this file is modified in a PR without a matching key rotation ticket
- `ModelIntegrityError` must cause `ModelInference.__init__()` to raise and the inference service to fail to start — it must never be caught silently; this is a hard security boundary
- Sidecar `.sig` files must be validated for JSON structure before attempting signature verification; a malformed `.sig` file that causes a JSON parse error should raise `ModelIntegrityError`, not a generic `JSONDecodeError`
- Do not use SHA-1 or MD5 for the content digest; SHA-256 is the minimum required hash algorithm

## Testing Requirements
- Unit tests covering:
  - `sign_artifact()` creates `.sig` file with correct JSON structure
  - `verify_artifact()` returns `True` for a freshly signed artifact
  - `verify_artifact()` raises `ModelIntegrityError` when model file is modified after signing (append 1 byte)
  - `verify_artifact()` raises `ModelIntegrityError` when `.sig` file is absent
  - `verify_artifact()` raises `ModelIntegrityError` when signature is base64-corrupted
- Integration tests covering:
  - Full training run produces `.sig` sidecar for every model artifact
  - `ModelInference.__init__()` starts successfully when all signatures are valid
  - `ModelInference.__init__()` raises `ModelIntegrityError` when one model file is tampered
  - `cli.py verify-models` prints VALID for all signed artifacts
- Edge cases:
  - `LEDGERLENS_MODEL_SIGNING_KEY` not set: `sign_artifact()` raises `ConfigurationError` with clear message
  - `config/model_signing_pubkey.pem` absent: `verify_artifact()` raises `ModelIntegrityError` with clear message
  - Model file is empty (0 bytes): signing succeeds; SHA-256 of empty bytes is valid

## Documentation Requirements
- Create `detection/model_signing.py` with full module docstring covering the signing protocol and key management workflow
- Add `LEDGERLENS_MODEL_SIGNING_KEY` to `.env.example` with a comment "# Ed25519 private key PEM (training environment only; never set in production inference environment)"
- Add `config/model_signing_pubkey.pem` as a placeholder file with a comment
- Create `docs/model_signing.md` with key generation instructions, signing workflow, key rotation procedure, and threat model

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: Python `cryptography` library, Ed25519, supply chain security, MLOps artifact management
- Your approach or initial thoughts on key management for CI/CD environments
- Estimated time to complete

**Ideal contributor profile:** Security-minded Python engineer with experience implementing cryptographic artifact signing in ML pipelines; familiarity with supply chain attack vectors against ML systems (e.g., SolarWinds-style model replacement) is highly valuable.
