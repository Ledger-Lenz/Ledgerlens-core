FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c \
    "import tomllib; f=open('pyproject.toml','rb'); print(tomllib.load(f)['project']['version'])" \
    > /tmp/version.txt

# ─── Final image ───────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /tmp/version.txt /tmp/version.txt

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Set OCI image labels from the version file
ARG BUILD_VERSION="0.0.0"
LABEL org.opencontainers.image.title="ledgerlens-core"
LABEL org.opencontainers.image.description="Benford's Law + ensemble ML wash-trading detection engine"
LABEL org.opencontainers.image.version="${BUILD_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Ledger-Lenz/Ledgerlens-core"

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
