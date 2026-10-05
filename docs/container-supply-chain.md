# Container Supply Chain: Reproducible, Signed Images

The `ledgerlens` image published by `.github/workflows/cd.yml` is built from a
multi-stage `Dockerfile`, signed with [cosign](https://docs.sigstore.dev/), and
published with SLSA build provenance and an SBOM.

## Build hardening

- **Base images pinned by digest.** Both the builder and runtime stages use
  `ARG PYTHON_IMAGE=python:3.12-slim@sha256:…`. To rebase, resolve the new
  digest (`docker buildx imagetools inspect python:3.12-slim`) and update that
  single `ARG`.
- **Multi-stage, minimal runtime.** Compilers (`gcc`, `g++`, `libffi-dev`) exist
  only in the builder stage; the runtime copies installed packages from
  `/install`. `.dockerignore` keeps tests, docs, and the non-Python build trees
  (Rust, Go, TypeScript SDKs) out of the build context and the final image.
- **Reproducible.** `SOURCE_DATE_EPOCH` (set to the commit timestamp in CI)
  plus BuildKit's `rewrite-timestamp=true` clamps file and layer timestamps.
  Wheels are installed with `--no-compile`, because `.pyc` files embed source
  mtimes.

## Verifying a published image

Images are signed keylessly through GitHub OIDC, so there is no public key to
distribute. Verification checks the signing workflow identity:

```bash
IMAGE=docker.io/<org>/ledgerlens@sha256:<digest>

cosign verify "$IMAGE" \
  --certificate-identity-regexp '^https://github.com/Ledger-Lenz/Ledgerlens-core/\.github/workflows/cd\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# GitHub build provenance attestation (SLSA v1)
gh attestation verify "oci://$IMAGE" --repo Ledger-Lenz/Ledgerlens-core

# BuildKit provenance and SBOM attached to the image index
docker buildx imagetools inspect "$IMAGE" --format '{{ json .Provenance }}'
docker buildx imagetools inspect "$IMAGE" --format '{{ json .SBOM }}'
```

Always deploy by digest (`@sha256:…`), not by tag, so the verified artifact is
the one that runs. The Helm deploy step already records `imageDigest`.

## Reproducibility check

`.github/workflows/reproducible-build.yml` builds the `runtime` target twice
from scratch (`--no-cache`) using the same `SOURCE_DATE_EPOCH` and fails if the
two image digests differ. Run it locally:

```bash
EPOCH=$(git log -1 --format=%ct)
for n in 1 2; do
  docker buildx build --no-cache --target runtime \
    --build-arg SOURCE_DATE_EPOCH=$EPOCH \
    --output type=oci,dest=/tmp/img$n.tar,rewrite-timestamp=true \
    --metadata-file /tmp/meta$n.json .
done
jq -r '."containerimage.digest"' /tmp/meta1.json /tmp/meta2.json
```

**Known limits.** Two builds of the same commit at close points in time match.
Builds weeks apart can still differ, because `apt-get` pulls the latest Debian
package versions and `requirements/*.txt` use `>=` ranges. Byte-identical
rebuilds across time need a hash-locked requirements file
(`pip-compile --generate-hashes`) and a snapshot apt mirror
(`snapshot.debian.org`). Until then, the signed provenance attestation, not
bit-for-bit rebuilding, is the source of truth for what went into a release.
