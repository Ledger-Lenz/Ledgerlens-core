# Model Signing (ED25519)

## Threat Model

LedgerLens ensemble models (Random Forest, XGBoost, LightGBM) are serialised as
`.joblib` files. Python's `joblib.load()` can execute arbitrary code during
deserialisation via the `__reduce__` protocol. An attacker who gains write access
to the `models/` directory (e.g., compromised CI, container escape, misconfigured
volume mount) could replace a `.joblib` file with a malicious serialised object.

ED25519 model signing provides:

- **Integrity**: a valid signature proves the file has not been modified since training.
- **Authenticity**: a valid signature proves the file was produced by the LedgerLens
  training pipeline (which holds the private key).

## Signing Scheme

1. **At training time**: compute `SHA-256(model_bytes)`, sign the digest with the
   ED25519 private key, write the base64-encoded signature to `<model>.joblib.sig`.
2. **At load time**: recompute `SHA-256(model_bytes)`, read the `.sig` file, verify
   the signature against the public key from `config/settings.py`. If verification
   fails, raise `ModelIntegrityError` and abort inference.

## Key Management

| Key | Location | Secret? |
|-----|----------|---------|
| Public key | `MODEL_SIGNING_PUBLIC_KEY` in `config/settings.py` | No — auditable in source control |
| Private key | `MODEL_SIGNING_PRIVATE_KEY` environment variable | Yes — never written to disk |

### Initial Setup

```bash
python cli.py generate-signing-key
```

This prints both keys to stdout. Copy the public key into your `.env` file as
`MODEL_SIGNING_PUBLIC_KEY` and store the private key in your secret manager as
`MODEL_SIGNING_PRIVATE_KEY`.

## Key Rotation

1. Generate a new keypair: `python cli.py generate-signing-key`
2. Retrain all models with the new private key set in the environment.
3. Update `MODEL_SIGNING_PUBLIC_KEY` in settings (requires code review).
4. Commit and deploy.
5. Retire the old private key.

## CI Integration

Add a step that runs **before** any scoring:

```yaml
- name: Verify model signatures
  run: python cli.py verify-models
```

This exits non-zero if any `.joblib` file in `MODEL_DIR` has a missing or invalid
signature.

## CLI Commands

| Command | Description |
|---------|-------------|
| `generate-signing-key` | Generate a new ED25519 keypair for model signing |
| `verify-models` | Verify all `.joblib` signatures in `MODEL_DIR` |

## FAQ

**Q: What happens if I deploy without setting the keys?**
A: `verify-models` will fail with a clear error. Model loading at inference time
will also fail if the public key is not configured.

**Q: Can I use the legacy HMAC-SHA256 signing alongside ED25519?**
A: Yes. The HMAC signing (`sign_model_file` / `verify_model_file`) remains for
backward compatibility. The ED25519 `ModelSigner` class is the recommended path.

**Q: What if `.sig` files are missing from the models directory?**
A: `verify-models` will fail loudly. Ensure `.sig` files are committed alongside
`.joblib` files or generated in CI after training.
