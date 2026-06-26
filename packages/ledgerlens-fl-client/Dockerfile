FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml .
COPY src ./src

RUN pip install --no-cache-dir build && \
    python -m build --wheel

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir ledgerlens-fl-client==0.1.0 || true

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl

ENV FL_SERVER_URL=https://fl.ledgerlens.io
ENV FL_API_KEY=""
ENV FL_DATA_DIR=/data
ENV FL_OPERATOR_ID=""
ENV FL_ROUNDS=1

LABEL org.opencontainers.image.title="ledgerlens-fl-client"
LABEL org.opencontainers.image.description="LedgerLens federated learning client for exchange-side participation"
LABEL org.opencontainers.image.source="https://github.com/Ledger-Lenz/Ledgerlens-core"

CMD ["python", "-m", "ledgerlens_fl_client"]