"""Tests for detection/gnn_ring_detector.py — Issue #295.

Covers:
- build_transaction_graph produces correct node/edge counts.
- GraphSAGEEncoder forward pass shape.
- RingMembershipClassifier output range [0, 1].
- GNNRingDetector.predict SCC fallback (model not fitted).
- GNNRingDetector.predict returns 0.0 when not fitted and fallback=False.
- top_neighbours returns exactly k wallets sorted by descending cosine similarity.
- Checksum mismatch triggers fallback, not exception.
- Integration: GET /gnn/ring-score/{wallet} returns correct schema.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from types import SimpleNamespace

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_trade(base: str, counter: str, amount: float = 100.0, ts: float = 0.0):
    return SimpleNamespace(
        base_account=base,
        counter_account=counter,
        base_amount=amount,
        ledger_close_time_ts=ts,
        base_asset_code="XLM",
        counter_asset_code="USDC",
    )


def _default_node_fn(wallet: str):
    """Simple 4-dim node feature vector."""
    try:
        import torch
        h = abs(hash(wallet)) % 10000
        return torch.tensor(
            [h / 10000.0, len(wallet) / 60.0, float(wallet.startswith("G")), 0.0],
            dtype=torch.float,
        )
    except ImportError:
        return [0.0] * 4


# ── PyG availability guard ────────────────────────────────────────────────────

try:
    import torch  # noqa: F401
    from torch_geometric.data import HeteroData  # noqa: F401
    from detection.gnn_ring_detector import (
        GraphSAGEEncoder,
        RingMembershipClassifier,
        build_transaction_graph,
        GNNRingDetector,
        _verify_model_checksum,
    )
    _HAS_PYG = True
except ImportError:
    _HAS_PYG = False

pytestmark = pytest.mark.skipif(
    not _HAS_PYG, reason="PyTorch Geometric not installed — skipping GNN tests."
)


# ── Test: build_transaction_graph ─────────────────────────────────────────────

class TestBuildTransactionGraph:
    def test_correct_node_count(self):
        wallets = [f"G{'A'*54}{i}" for i in range(10)]
        trades = [
            _make_trade(wallets[i % 10], wallets[(i + 1) % 10], ts=float(i))
            for i in range(20)
        ]
        graph = build_transaction_graph(trades, _default_node_fn)
        assert graph["wallet"].x.shape[0] == 10

    def test_correct_edge_count(self):
        wallets = [f"G{'B'*54}{i}" for i in range(5)]
        trades = [_make_trade(wallets[0], wallets[i], ts=float(i)) for i in range(1, 5)]
        graph = build_transaction_graph(trades, _default_node_fn)
        assert graph["wallet", "trades", "wallet"].edge_index.shape[1] == 4

    def test_empty_trades(self):
        graph = build_transaction_graph([], _default_node_fn)
        assert graph["wallet"].x.shape[0] == 0
        assert graph["wallet", "trades", "wallet"].edge_index.shape[1] == 0

    def test_wallet_list_stored(self):
        wallets = [f"G{'C'*54}{i}" for i in range(3)]
        trades = [_make_trade(wallets[0], wallets[1])]
        graph = build_transaction_graph(trades, _default_node_fn)
        assert len(graph["wallet"].wallet_list) == 2


# ── Test: GraphSAGEEncoder ────────────────────────────────────────────────────

class TestGraphSAGEEncoder:
    def test_output_shape(self):
        wallets = [f"G{'D'*54}{i}" for i in range(8)]
        trades = [_make_trade(wallets[i], wallets[(i + 1) % 8]) for i in range(8)]
        graph = build_transaction_graph(trades, _default_node_fn)
        in_channels = graph["wallet"].x.shape[1]
        encoder = GraphSAGEEncoder(in_channels=in_channels, out_channels=64)
        out = encoder(graph["wallet"].x, graph["wallet", "trades", "wallet"].edge_index)
        assert out.shape == (8, 64)

    def test_single_node(self):
        """Encoder should handle a single-node graph."""
        import torch
        encoder = GraphSAGEEncoder(in_channels=4, out_channels=64)
        x = torch.zeros((1, 4))
        edge_index = torch.zeros((2, 0), dtype=torch.long)
        out = encoder(x, edge_index)
        assert out.shape == (1, 64)


# ── Test: RingMembershipClassifier ────────────────────────────────────────────

class TestRingMembershipClassifier:
    def test_output_in_range(self):
        import torch
        clf = RingMembershipClassifier(embedding_dim=64)
        embeddings = torch.randn(20, 64)
        scores = clf(embeddings)
        assert scores.shape == (20,)
        assert float(scores.min()) >= 0.0
        assert float(scores.max()) <= 1.0

    def test_single_embedding(self):
        import torch
        clf = RingMembershipClassifier(embedding_dim=64)
        scores = clf(torch.randn(1, 64))
        assert 0.0 <= float(scores[0]) <= 1.0


# ── Test: GNNRingDetector fallback ────────────────────────────────────────────

class TestGNNRingDetectorFallback:
    def _build_graph_with_scc(self, wallet: str):
        wallets = [wallet, f"G{'E'*54}1"]
        trades = [_make_trade(wallets[0], wallets[1])]
        graph = build_transaction_graph(trades, _default_node_fn)
        # Inject SCC membership: wallet=1, other=0
        graph["wallet"].scc_membership = [1, 0]
        return graph

    def test_predict_scc_fallback_true(self):
        wallet = f"G{'F'*55}"
        graph = self._build_graph_with_scc(wallet)
        detector = GNNRingDetector(fallback_to_scc=True)
        score = detector.predict(wallet, graph)
        # Not fitted → falls back to SCC: wallet index 0, scc_membership[0]=1
        assert score == 1.0

    def test_predict_scc_fallback_false(self):
        wallet = f"G{'G'*55}"
        trades = [_make_trade(wallet, f"G{'H'*55}")]
        graph = build_transaction_graph(trades, _default_node_fn)
        detector = GNNRingDetector(fallback_to_scc=False)
        assert detector.predict(wallet, graph) == 0.0

    def test_predict_unknown_wallet_fallback_false(self):
        detector = GNNRingDetector(fallback_to_scc=False)
        trades = [_make_trade(f"G{'I'*55}", f"G{'J'*55}")]
        graph = build_transaction_graph(trades, _default_node_fn)
        assert detector.predict(f"G{'K'*55}", graph) == 0.0


# ── Test: top_neighbours ──────────────────────────────────────────────────────

class TestTopNeighbours:
    def test_returns_empty_when_not_fitted(self):
        detector = GNNRingDetector(fallback_to_scc=True)
        assert detector.top_neighbours(f"G{'L'*55}", None, k=5) == []

    def test_returns_k_wallets_when_fitted(self):
        """After loading a model, top_neighbours returns exactly k wallets."""
        wallets = [f"G{'M'*54}{i}" for i in range(10)]
        trades = [_make_trade(wallets[i], wallets[(i + 1) % 10]) for i in range(10)]
        graph = build_transaction_graph(trades, _default_node_fn)
        in_channels = graph["wallet"].x.shape[1]

        detector = GNNRingDetector(fallback_to_scc=False)
        detector._encoder = GraphSAGEEncoder(in_channels=in_channels, out_channels=64)
        detector._classifier = RingMembershipClassifier(embedding_dim=64)
        detector._fitted = True

        neighbours = detector.top_neighbours(wallets[0], graph, k=5)
        assert len(neighbours) == 5
        # All returned wallets should be in the graph wallet list
        for n in neighbours:
            assert n in graph["wallet"].wallet_list
        # Target wallet itself should NOT be in results
        assert wallets[0] not in neighbours


# ── Test: checksum verification ───────────────────────────────────────────────

class TestChecksumVerification:
    def test_mismatch_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = os.path.join(tmpdir, "model.pt")
            checksum_path = os.path.join(tmpdir, "model.sha256")
            with open(model_path, "wb") as f:
                f.write(b"fake model content")
            with open(checksum_path, "w") as f:
                f.write("0000000000000000000000000000000000000000000000000000000000000000\n")
            result = _verify_model_checksum(model_path, checksum_path)
            assert result is False

    def test_correct_checksum_returns_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = os.path.join(tmpdir, "model.pt")
            checksum_path = os.path.join(tmpdir, "model.sha256")
            content = b"valid model bytes"
            with open(model_path, "wb") as f:
                f.write(content)
            h = hashlib.sha256(content).hexdigest()
            with open(checksum_path, "w") as f:
                f.write(h + "\n")
            result = _verify_model_checksum(model_path, checksum_path)
            assert result is True

    def test_mismatch_triggers_scc_fallback(self):
        """GNNRingDetector.load() with bad checksum leaves _fitted=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = os.path.join(tmpdir, "gnn_ring_detector.pt")
            checksum_path = model_path.replace(".pt", ".sha256")
            with open(model_path, "wb") as f:
                f.write(b"bad content")
            with open(checksum_path, "w") as f:
                f.write("0000000000000000000000000000000000000000000000000000000000000000\n")
            detector = GNNRingDetector(model_path=model_path)
            detector.load()
            assert not detector._fitted


# ── Integration: API endpoint ──────────────────────────────────────────────────

class TestGNNAPIEndpoint:
    def test_ring_score_schema(self):
        from fastapi.testclient import TestClient
        from api.main import app  # type: ignore

        client = TestClient(app)
        wallet = "G" + "A" * 55
        resp = client.get(f"/gnn/ring-score/{wallet}")
        # Should return 200 with correct schema fields
        assert resp.status_code == 200
        body = resp.json()
        assert "ring_membership_score" in body
        assert "top_neighbours" in body
        assert "model_fitted" in body
        assert "fallback_used" in body
        assert isinstance(body["ring_membership_score"], float)
        assert isinstance(body["top_neighbours"], list)

    def test_invalid_wallet_returns_400(self):
        from fastapi.testclient import TestClient
        from api.main import app  # type: ignore

        client = TestClient(app)
        resp = client.get("/gnn/ring-score/invalid_wallet")
        assert resp.status_code == 400
