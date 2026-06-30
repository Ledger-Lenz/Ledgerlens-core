# ─── Builder stage ────────────────────────────────────────────────────────────
# Installs all build-time dependencies and compiles wheels.  Build tools
# (gcc, make, etc.) never reach the final image.
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml requirements.txt ./

RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

RUN python -c \
    "import tomllib; f=open('pyproject.toml','rb'); print(tomllib.load(f)['project']['version'])" \
    > /tmp/version.txt

# ─── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim

ARG BUILD_VERSION="0.0.0"

LABEL org.opencontainers.image.title="ledgerlens-core"
LABEL org.opencontainers.image.description="Benford's Law + ensemble ML wash-trading detection engine"
LABEL org.opencontainers.image.version="${BUILD_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Ledger-Lenz/Ledgerlens-core"

RUN groupadd --gid 1000 ledgerlens && \
    useradd --uid 1000 --gid ledgerlens --shell /bin/bash --create-home ledgerlens

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /tmp/version.txt /tmp/version.txt

COPY --chown=ledgerlens:ledgerlens . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

USER ledgerlens

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
