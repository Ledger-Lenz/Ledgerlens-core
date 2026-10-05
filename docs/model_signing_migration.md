# Model Signing Migration: Sigstore/cosign Provenance

> **Issue #934** — Migrate model signing to Sigstore/cosign-based provenance.

## Overview

New model artifacts **must** be signed via [Sigstore](https://www.sigstore.dev/)
keyless signing, which records a transparency-log entry in
[Rekor](https://rekor.sigstore.dev).  This provides independently verifiable,
third-party-auditable provenance without requiring pre-distributed key material.

`model_inference.py` calls `verify_sigstore_attestation()` before loading any
model artifact.  Loading is **refused** when no valid `.sigstore` bundle exists.

---

## Prerequisites

```bash
pip install sigstore
```

You also need a valid OIDC identity token.  Supported providers:

| Environment | Provider |
|---|---|
| GitHub Actions | `ACTIONS_ID_TOKEN_REQUEST_URL` / `ACTIONS_ID_TOKEN_REQUEST_TOKEN` (automatic) |
| Google Cloud | Workload Identity Federation |
| Local dev | Sigstore's interactive browser-based flow (`sigstore sign --interactive`) |

---

## Signing New Models

```python
from pathlib import Path
from detection.model_signing import SigstoreSigner

# After training and saving the model artifact:
model_path = Path("models/random_forest_v1a2b3c4.joblib")
bundle_path = SigstoreSigner.sign(model_path)
# Produces: models/random_forest_v1a2b3c4.joblib.sigstore
```

The `.sigstore` bundle file must be committed/stored alongside the model artifact.

---

## Verifying a Model Artifact

```python
from detection.model_signing import SigstoreSigner
from pathlib import Path

SigstoreSigner.verify(
    Path("models/random_forest_v1a2b3c4.joblib"),
    expected_identity="ci@my-org.iam.gserviceaccount.com",  # optional
    expected_issuer="https://accounts.google.com",           # optional
)
```

For external auditors using the `cosign` CLI:

```bash
cosign verify-blob \
  --bundle models/random_forest_v1a2b3c4.joblib.sigstore \
  --certificate-identity ci@my-org.iam.gserviceaccount.com \
  --certificate-oidc-issuer https://accounts.google.com \
  models/random_forest_v1a2b3c4.joblib
```

---

## Migrating Existing Signed Artifacts

Run the migration script to re-sign all existing HMAC/ED25519-signed models in
a model directory with Sigstore:

```bash
python -m detection.model_signing migrate ./models
```

This script:
1. Scans `./models` for `*.joblib` files.
2. Skips any that already have a valid `.sigstore` bundle.
3. Signs each remaining artifact with `SigstoreSigner.sign()`.
4. Logs a summary of signed and skipped artifacts.

The script is idempotent — re-running it is safe.

---

## Disabling Enforcement (Dev/Local Only)

Set `SIGSTORE_ENFORCEMENT_ENABLED = False` in `detection/model_signing.py`
**or** monkeypatch it in tests to skip attestation checks locally.  This flag
**must** be `True` in all production environments.

---

## CI/CD Integration

In GitHub Actions, add the following to your workflow after model training:

```yaml
- name: Sign model artifacts
  run: |
    pip install sigstore
    python - <<'EOF'
    from pathlib import Path
    from detection.model_signing import SigstoreSigner
    for p in Path("models").glob("*.joblib"):
        SigstoreSigner.sign(p)
        print(f"Signed {p.name}")
    EOF
  env:
    ACTIONS_ID_TOKEN_REQUEST_URL: ${{ env.ACTIONS_ID_TOKEN_REQUEST_URL }}
    ACTIONS_ID_TOKEN_REQUEST_TOKEN: ${{ env.ACTIONS_ID_TOKEN_REQUEST_TOKEN }}
```

The Rekor transparency-log entry URL is printed by `sigstore sign` and can be
archived as a workflow artifact for audit purposes.

---

## Transparency Log Entry

Each signed model produces a Rekor log entry queryable at:

```
https://rekor.sigstore.dev/api/v1/log/entries?logIndex=<INDEX>
```

The log index is embedded in the `.sigstore` bundle under
`verificationMaterial.tlogEntries[0].logIndex`.

---

*Content was rephrased for compliance with licensing restrictions.*
