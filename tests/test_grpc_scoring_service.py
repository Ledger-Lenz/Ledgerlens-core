"""Tests for gRPC Internal Scoring Service (Issue #338)."""

from datetime import datetime, timezone
import pytest
import grpc

from config.settings import settings
from detection.api_key_store import create_api_key, _init_table
from detection.risk_score import RiskScore
from detection.storage import save_scores
from generated import scoring_pb2, scoring_pb2_grpc
from api.grpc_scoring_service import create_grpc_server, _to_proto


@pytest.fixture(scope="module")
def grpc_server_fixture(tmp_path_factory):
    db_file = str(tmp_path_factory.mktemp("grpc_db") / "test_ledgerlens.db")
    original_db = settings.ledgerlens_db_path
    original_insecure = settings.grpc_allow_insecure
    
    settings.ledgerlens_db_path = db_file
    settings.grpc_allow_insecure = True
    _init_table()

    server, port = create_grpc_server(port=0)
    server.start()

    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = scoring_pb2_grpc.ScoringServiceStub(channel)

    yield {
        "server": server,
        "channel": channel,
        "stub": stub,
        "port": port,
        "db_path": db_file,
    }

    channel.close()
    server.stop(0)
    settings.ledgerlens_db_path = original_db
    settings.grpc_allow_insecure = original_insecure


def test_score_wallet_parity(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    db_path = grpc_server_fixture["db_path"]

    wallet = "GABC1234567890WXYZ"
    asset_pair = "XLM/USDC"
    ts = datetime.now(timezone.utc)
    score_obj = RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=85,
        benford_flag=True,
        ml_flag=False,
        confidence=90,
        timestamp=ts,
        score_lower=80.0,
        score_upper=90.0,
        coverage_guarantee=0.9,
    )

    # Test direct RiskScore -> RiskScoreProto conversion with optional conformal fields
    proto = _to_proto(score_obj)
    assert proto.score_lower == pytest.approx(80.0)
    assert proto.score_upper == pytest.approx(90.0)
    assert proto.coverage_guarantee == pytest.approx(0.9)

    save_scores([score_obj], db_path=db_path)

    key_meta = create_api_key(scopes=["read:scores"])
    api_key = key_meta["plaintext_key"]

    request = scoring_pb2.ScoreRequest(wallet=wallet, asset_pair=asset_pair)
    response = stub.ScoreWallet(
        request, metadata=[("x-ledgerlens-api-key", api_key)]
    )

    assert response.wallet == wallet
    assert response.asset_pair == asset_pair
    assert response.score == 85
    assert response.benford_flag is True
    assert response.ml_flag is False
    assert response.confidence == 90


def test_unauthenticated_missing_or_invalid_key(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    request = scoring_pb2.ScoreRequest(wallet="GABC1234567890WXYZ")

    # Missing metadata
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ScoreWallet(request)
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    # Invalid key
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", "invalid_key_123")])
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_permission_denied_wrong_scope(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    key_meta = create_api_key(scopes=["write:suppressions"])
    api_key = key_meta["plaintext_key"]

    request = scoring_pb2.ScoreRequest(wallet="GABC1234567890WXYZ")
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert "read:scores" in exc_info.value.details()


def test_not_found_masked_wallet(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    key_meta = create_api_key(scopes=["read:scores"])
    api_key = key_meta["plaintext_key"]

    request = scoring_pb2.ScoreRequest(wallet="GNOTFOUND1234567890WXYZ")
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND
    assert "GNOTFOUN...WXYZ" in exc_info.value.details()


def test_batch_score_wallets_order(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    db_path = grpc_server_fixture["db_path"]

    scores = []
    wallets = [f"GWALLET_{i:03d}_12345678" for i in range(50)]
    ts = datetime.now(timezone.utc)
    for w in wallets:
        scores.append(
            RiskScore(
                wallet=w,
                asset_pair="XLM/USDC",
                score=50,
                benford_flag=False,
                ml_flag=True,
                confidence=80,
                timestamp=ts,
            )
        )
    save_scores(scores, db_path=db_path)

    key_meta = create_api_key(scopes=["read:scores"])
    api_key = key_meta["plaintext_key"]

    def req_generator():
        for w in wallets:
            yield scoring_pb2.ScoreRequest(wallet=w, asset_pair="XLM/USDC")

    responses = list(
        stub.BatchScoreWallets(
            req_generator(), metadata=[("x-ledgerlens-api-key", api_key)]
        )
    )

    assert len(responses) == 50
    for i, resp in enumerate(responses):
        assert resp.wallet == wallets[i]


def test_batch_max_batch_exceeded(grpc_server_fixture, monkeypatch):
    stub = grpc_server_fixture["stub"]
    key_meta = create_api_key(scopes=["read:scores"])
    api_key = key_meta["plaintext_key"]

    monkeypatch.setattr(settings, "grpc_max_batch_wallets", 5)

    def req_generator():
        for i in range(6):
            yield scoring_pb2.ScoreRequest(wallet=f"GEXCEED_{i}")

    with pytest.raises(grpc.RpcError) as exc_info:
        list(
            stub.BatchScoreWallets(
                req_generator(), metadata=[("x-ledgerlens-api-key", api_key)]
            )
        )
    assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_rate_limit_exceeded_shared_counter(grpc_server_fixture):
    stub = grpc_server_fixture["stub"]
    db_path = grpc_server_fixture["db_path"]
    wallet = "GRATELIMIT_WALLET_1234"

    save_scores(
        [
            RiskScore(
                wallet=wallet,
                asset_pair="XLM/USDC",
                score=10,
                benford_flag=False,
                ml_flag=False,
                confidence=90,
                timestamp=datetime.now(timezone.utc),
            )
        ],
        db_path=db_path,
    )

    key_meta = create_api_key(scopes=["read:scores"], rate_limit_per_minute=2)
    api_key = key_meta["plaintext_key"]
    request = scoring_pb2.ScoreRequest(wallet=wallet)

    # First 2 requests succeed
    r1 = stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
    assert r1.wallet == wallet

    r2 = stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
    assert r2.wallet == wallet

    # 3rd request fails with RESOURCE_EXHAUSTED
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.ScoreWallet(request, metadata=[("x-ledgerlens-api-key", api_key)])
    assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_tls_required_by_default(monkeypatch):
    monkeypatch.setattr(settings, "grpc_allow_insecure", False)
    monkeypatch.setattr(settings, "grpc_tls_cert_path", "")
    monkeypatch.setattr(settings, "grpc_tls_key_path", "")

    with pytest.raises(ValueError) as exc_info:
        create_grpc_server(port=0)
    assert "TLS credentials required" in str(exc_info.value)
