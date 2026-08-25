"""Tests for the generated gRPC client proof of concept (Issue #691)."""

from scripts.grpc_client_poc import _score_to_dict, score_wallets
from generated import scoring_pb2


class FakeScoringStub:
    """Minimal stub double that records the generated-client call contract."""

    def __init__(self) -> None:
        self.unary_wallets: list[str] = []
        self.stream_wallets: list[str] = []

    def ScoreWallet(self, request, *, metadata, timeout):
        assert metadata == (("x-ledgerlens-api-key", "test-key"),)
        assert timeout == 2.5
        self.unary_wallets.append(request.wallet)
        return scoring_pb2.RiskScoreProto(
            wallet=request.wallet,
            asset_pair="XLM/USDC",
            score=82,
            confidence=91,
            timestamp="2026-08-25T00:00:00Z",
            score_lower=80.0,
        )

    def BatchScoreWallets(self, requests, *, metadata, timeout):
        assert metadata == (("x-ledgerlens-api-key", "test-key"),)
        assert timeout == 2.5
        requests = list(requests)
        self.stream_wallets = [request.wallet for request in requests]
        return iter(
            scoring_pb2.RiskScoreProto(wallet=request.wallet, score=50)
            for request in requests
        )


def test_score_wallets_calls_unary_and_streaming_rpcs():
    stub = FakeScoringStub()

    unary, streaming = score_wallets(stub, ["GAAA", "GBBB"], "test-key", 2.5)

    assert stub.unary_wallets == ["GAAA", "GBBB"]
    assert stub.stream_wallets == ["GAAA", "GBBB"]
    assert [score.wallet for score in unary] == ["GAAA", "GBBB"]
    assert [score.wallet for score in streaming] == ["GAAA", "GBBB"]


def test_score_to_dict_preserves_optional_proto_field_presence():
    score = scoring_pb2.RiskScoreProto(
        wallet="GAAA",
        score=82,
        score_lower=80.0,
    )

    result = _score_to_dict(score)

    assert result["score_lower"] == 80.0
    assert "score_upper" not in result
    assert "coverage_guarantee" not in result
