"""Tests for structured JSON logging and correlation ID threading."""

import json
import logging
import uuid

import pytest
from starlette.testclient import TestClient


def _setup_logging_capture(capfd):
    """Configure logging and return (logger, capfd)."""
    from config.logging_config import configure_logging
    configure_logging("test-service", log_level="INFO")
    return logging.getLogger("test.structured_logging")


# ---------------------------------------------------------------------------
# (a) Every log record is valid JSON with required fields
# ---------------------------------------------------------------------------

def test_log_records_are_valid_json(capfd):
    logger = _setup_logging_capture(capfd)
    logger.info("hello world")
    captured = capfd.readouterr()
    for line in captured.err.strip().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        assert "timestamp" in data
        assert "level" in data
        assert "event" in data or "message" in data


# ---------------------------------------------------------------------------
# (b) Every record includes correlation_id, timestamp, level
# ---------------------------------------------------------------------------

def test_log_records_include_required_fields(capfd):
    from config.correlation import set_correlation_id
    cid = str(uuid.uuid4())
    set_correlation_id(cid)

    logger = _setup_logging_capture(capfd)
    logger.info("checking fields")
    captured = capfd.readouterr()

    line = next(l for l in captured.err.strip().splitlines() if l.strip())
    data = json.loads(line)
    assert data.get("correlation_id") == cid
    assert "timestamp" in data
    assert "level" in data


# ---------------------------------------------------------------------------
# (c) No full wallet address appears in INFO-level log output
# ---------------------------------------------------------------------------

def test_no_full_wallet_in_logs(capfd):
    import re
    from config.correlation import mask_wallet

    wallet = "GABC1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234WXYZ"
    masked = mask_wallet(wallet)

    logger = _setup_logging_capture(capfd)
    logger.info("Processing wallet %s", masked)
    captured = capfd.readouterr()

    stellar_pattern = re.compile(r"G[A-Z2-7]{55}")
    for line in captured.err.strip().splitlines():
        assert not stellar_pattern.search(line), f"Full wallet address found in log: {line}"


def test_mask_wallet():
    from config.correlation import mask_wallet

    wallet = "GABC1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234WXYZ"
    masked = mask_wallet(wallet)
    assert masked == "GABC1234...WXYZ"
    assert len(masked) < len(wallet)


def test_mask_wallet_short():
    from config.correlation import mask_wallet
    assert mask_wallet("GABC") == "GABC"
    assert mask_wallet("") == ""


# ---------------------------------------------------------------------------
# (d) CorrelationIDMiddleware propagates X-Correlation-ID header to response
# ---------------------------------------------------------------------------

def test_correlation_id_middleware_propagates_header():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from config.correlation import CorrelationIDMiddleware, get_correlation_id

    test_app = FastAPI()
    test_app.add_middleware(CorrelationIDMiddleware)

    @test_app.get("/ping")
    def ping():
        return JSONResponse({"cid": get_correlation_id()})

    client = TestClient(test_app, raise_server_exceptions=True)

    # With explicit correlation ID
    cid = str(uuid.uuid4())
    resp = client.get("/ping", headers={"X-Correlation-ID": cid})
    assert resp.status_code == 200
    assert resp.headers["X-Correlation-ID"] == cid
    assert resp.json()["cid"] == cid


def test_correlation_id_middleware_generates_if_absent():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from config.correlation import CorrelationIDMiddleware, get_correlation_id

    test_app = FastAPI()
    test_app.add_middleware(CorrelationIDMiddleware)

    @test_app.get("/ping")
    def ping():
        return JSONResponse({"cid": get_correlation_id()})

    client = TestClient(test_app, raise_server_exceptions=True)

    resp = client.get("/ping")
    assert resp.status_code == 200
    cid_in_header = resp.headers["X-Correlation-ID"]
    # Should be a valid UUID
    uuid.UUID(cid_in_header)
    assert resp.json()["cid"] == cid_in_header
