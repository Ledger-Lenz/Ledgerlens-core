"""Reproduces, then refutes, the same-process REST-vs-gRPC rate-limit bypass
described in the distributed-rate-limiting issue: a client alternating
between the REST `/v1/scores/{wallet}` endpoint and the gRPC `ScoreWallet`
RPC used to get ~2x its configured per-minute budget within a single
process, because `api/api_key_router.py`'s `require_scope` dependency (REST)
and `api/grpc_scoring_service.py`'s `_authenticate` (gRPC) each called
`detection.api_key_store.check_rate_limit`, which read/wrote its own
per-process-only in-memory dict shared by nothing else.

Both paths now call the same distributed limiter (detection/rate_limiter.py),
so this test asserts the combined REST+gRPC allowed count equals the
configured limit, not 2x it.
"""

from __future__ import annotations

import grpc
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("fakeredis")
pytest.importorskip("lupa")
import fakeredis  # noqa: E402

from unittest.mock import patch

from config.settings import settings
from detection.api_key_store import create_api_key, _init_table
from detection.rate_limiter import reset_rate_limiter
from generated import scoring_pb2, scoring_pb2_grpc
from api.grpc_scoring_service import create_grpc_server

WALLET = "G" + "A" * 55  # syntactically valid 56-char Stellar address


@pytest.fixture
def shared_backend(tmp_path, monkeypatch):
    """One shared fakeredis instance visible to both the REST TestClient
    calls and the in-process gRPC server's worker threads -- this is what
    makes the check meaningful: without it, each side would just get its
    own isolated fallback dict and the test would pass for the wrong reason.
    """
    db_file = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_file)

    original_db_path = settings.db_path
    original_quota_store = settings.gateway_quota_store
    original_grpc_insecure = settings.grpc_allow_insecure
    object.__setattr__(settings, "db_path", db_file)
    object.__setattr__(settings, "gateway_quota_store", "redis")
    object.__setattr__(settings, "grpc_allow_insecure", True)
    _init_table()

    fake_client = fakeredis.FakeStrictRedis()
    try:
        with patch("redis.from_url", return_value=fake_client):
            reset_rate_limiter()
            yield db_file
    finally:
        reset_rate_limiter()
        object.__setattr__(settings, "db_path", original_db_path)
        object.__setattr__(settings, "gateway_quota_store", original_quota_store)
        object.__setattr__(settings, "grpc_allow_insecure", original_grpc_insecure)


@pytest.fixture
def grpc_stub(shared_backend):
    server, port = create_grpc_server(port=0)
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = scoring_pb2_grpc.ScoringServiceStub(channel)
    yield stub
    channel.close()
    server.stop(0)


def test_rest_and_grpc_share_one_quota_no_2x_bypass(grpc_stub, shared_backend):
    from api.main import app

    key = create_api_key(scopes=["read:scores"], rate_limit_per_minute=10)
    api_key = key["plaintext_key"]

    client = TestClient(app)
    request = scoring_pb2.ScoreRequest(wallet=WALLET)

    allowed = 0
    denied = 0
    # Alternate REST / gRPC calls against the SAME key, well past the limit.
    for i in range(30):
        if i % 2 == 0:
            resp = client.get(f"/v1/scores/{WALLET}", headers={"X-LedgerLens-Api-Key": api_key})
            if resp.status_code == 429:
                denied += 1
            else:
                allowed += 1
        else:
            try:
                grpc_stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
                allowed += 1
            except grpc.RpcError as exc:
                if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    denied += 1
                else:
                    allowed += 1  # e.g. NOT_FOUND -- still passed the rate gate

    assert allowed == 10, (
        f"allowed={allowed} denied={denied} across combined REST+gRPC calls; "
        f"expected exactly the configured limit (10), not up to 2x it"
    )
    assert denied == 20


def test_rest_alone_hits_the_same_limit_grpc_would_have_shared(shared_backend):
    """Baseline: REST alone (no gRPC calls at all) is bounded by the
    configured limit -- establishes that the limit isn't being inflated by
    some other effect, isolating the cross-protocol sharing as the thing
    under test above."""
    from api.main import app

    key = create_api_key(scopes=["read:scores"], rate_limit_per_minute=10)
    api_key = key["plaintext_key"]
    client = TestClient(app)

    allowed = sum(
        client.get(f"/v1/scores/{WALLET}", headers={"X-LedgerLens-Api-Key": api_key}).status_code != 429
        for _ in range(15)
    )
    assert allowed == 10
