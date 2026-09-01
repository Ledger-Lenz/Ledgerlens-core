# ─── Builder stage ─────────────────────────────────────────────────────────
# Installs all build-time dependencies and compiles wheels.  Build tools
# (gcc, make, etc.) never reach the final image.
#
# Build targets:
#   docker build --target runtime .          ← default (base deps only)
#   docker build --target runtime-chain .    ← + EVM/chain deps (web3, k8s)
#   docker build --target runtime-ml .       ← + ML deps (mlflow, torch)
#   docker build --target dev .              ← all extras (local dev / CI)
FROM python:3.12-slim AS builder

# Pinned OS packages — update these in sync with security advisories
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only manifest + lockfiles first so that the pip install layer is cached
# when source code changes but dependencies do not.
COPY pyproject.toml requirements/base.txt ./
COPY requirements/ requirements/

# Upgrade pip/wheel to a known-good version, then install from the committed
# lockfile.  --require-hashes is not used here because the base.txt files in
# this repo use >= constraints (to be solved by the developer's pip-compile run);
# for fully hermetic container builds, run `make lock` first and commit the
# hash-annotated output from pip-compile --generate-hashes.
RUN pip install --upgrade pip==24.0 wheel==0.43.0 && \
    pip install --no-cache-dir --prefix=/install -r requirements/base.txt

# ─── Builder-chain stage ────────────────────────────────────────────────────
FROM builder AS builder-chain

RUN pip install --no-cache-dir --prefix=/install -r requirements/chain.txt

# ─── Builder-ml stage ───────────────────────────────────────────────────────
FROM builder AS builder-ml

RUN pip install --no-cache-dir --prefix=/install -r requirements/ml.txt

# ─── Builder-dev stage ──────────────────────────────────────────────────────
FROM builder AS builder-dev

RUN pip install --no-cache-dir --prefix=/install -r requirements/dev.txt

# ─── Runtime environment variables ──────────────────────────────────────────
# Every setting in config/settings.py has a safe default (see .env.example for
# the full annotated list), and the .env file is optional. The container
# therefore STARTS with no `-e` flags at all:
#
#   docker run -p 8000:8000 ledgerlens-core
#
# and `/health`, `/health/ready` and the read-only score endpoints work against
# a fresh ./ledgerlens.db SQLite file. There are NO strictly-required variables:
# nothing left unset aborts startup (verified by loading config.settings.Settings
# with an empty environment).
#
# Variables you almost certainly want to pass for a real deployment
# (env var -> config/settings.py field (default) -> effect if left unset):
#
#   LEDGERLENS_ADMIN_API_KEY -> ledgerlens_admin_api_key ("")
#       /admin/* and /metrics are UNAUTHENTICATED. Startup logs a
#       "SECURITY WARNING" but does not fail. Set this for any reachable deploy.
#   LEDGERLENS_DB_PATH -> ledgerlens_db_path ("./ledgerlens.db")
#       RiskScore store. Default lives inside the container and is lost on
#       recreate — point at a mounted volume to persist.
#   LEDGERLENS_CORS_ALLOWED_ORIGINS -> ledgerlens_cors_allowed_origins ("")
#       Empty = deny all browser origins. Set to your dashboard origin(s).
#       A literal "*" is rejected at startup.
#   NETWORK -> network ("testnet")
#       "testnet" | "mainnet". For mainnet also set NETWORK_PASSPHRASE and the
#       HORIZON_URL / HORIZON_STREAM_URL / SOROBAN_RPC_URL endpoints.
#
# Secrets — no default; the feature stays disabled or fails when invoked:
#   LEDGERLENS_SERVICE_SECRET_KEY      -> on-chain submit_score() calls fail auth
#   LEDGERLENS_WEBHOOK_ENCRYPTION_KEY  -> webhook subscriber secrets can't be stored
#   MODEL_SIGNING_PUBLIC_KEY           -> signed-model verification unavailable
#
# Variables that abort startup ONLY when set to an invalid value (never by being
# absent): NETWORK, GATEWAY_QUOTA_STORE ("sqlite"|"redis"), any *_URL (must be a
# valid http/https/redis URL), EVM_POOL_ADDRESSES (must be EIP-55 checksummed),
# and the DATA_DIR-relative paths (CURSOR_CHECKPOINT_PATH, HISTORICAL_PROGRESS_PATH
# must resolve inside DATA_DIR). See the @field_validator / @model_validator
# methods in config/settings.py for the full set.
# ────────────────────────────────────────────────────────────────────────────
# ─── Common runtime base ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime-base

ARG BUILD_VERSION="0.0.0"

LABEL org.opencontainers.image.title="ledgerlens-core"
LABEL org.opencontainers.image.description="Benford's Law + ensemble ML wash-trading detection engine"
LABEL org.opencontainers.image.version="${BUILD_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Ledger-Lenz/Ledgerlens-core"

RUN groupadd --gid 1000 ledgerlens && \
    useradd --uid 1000 --gid ledgerlens --shell /bin/bash --create-home ledgerlens

WORKDIR /app

# Build/runtime hygiene only — application config comes from the environment at
# runtime (see the "Runtime environment variables" note above), not from ENV here.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

COPY --chown=ledgerlens:ledgerlens . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

USER ledgerlens

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ─── Default runtime (base deps) ─────────────────────────────────────────────
FROM runtime-base AS runtime

COPY --from=builder /install /usr/local

# ─── Runtime with chain extras (EVM / kubernetes) ────────────────────────────
FROM runtime-base AS runtime-chain

COPY --from=builder-chain /install /usr/local

# ─── Runtime with ML extras (mlflow / torch) ─────────────────────────────────
FROM runtime-base AS runtime-ml

COPY --from=builder-ml /install /usr/local

# ─── Dev image (all extras + test/lint tools) ────────────────────────────────
FROM runtime-base AS dev

COPY --from=builder-dev /install /usr/local
