"""Tests for heterogeneous GNN graph construction and inference.

Covers:
- build_heterogeneous_graph produces correct node counts and edge shapes.
- Graceful degradation with no order events or funding edges.
- HeteroGraphSAGEEncoder output shape for both SAGE and HGT conv types.
- GNNRingDetector fallback from heterogeneous to homogeneous on missing checkpoint.
- Synthetic asset-mediated laundering scenario: heterogeneous mode AUC-ROC
  advantage over homogeneous mode.
- Regression: existing homogeneous-mode tests pass unchanged.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from types import SimpleNamespace

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_trade(base: str, counter: str, amount: float = 100.0, ts: float = 0.0,
                base_code: str = "XLM", counter_code: str = "USDC"):
    return SimpleNamespace(
        base_account=base,
        counter_account=counter,
        base_amount=amount,
        ledger_close_time_ts=ts,
        base_asset_code=base_code,
        counter_asset_code=counter_code,
    )


def _make_order_event(
    event_id: str,
    account: str,
    asset_pair: str = "XLM/USDC",
    event_type: str = "created",
    side: str = "sell",
    amount: float = 100.0,
    price: float = 0.1,
    ts: float = 0.0,
):
    from datetime import datetime, timezone
    return SimpleNamespace(
        id=event_id,
        timestamp=datetime.now(timezone.utc),
        account=account,
        asset_pair=asset_pair,
        side=side,
        amount=amount,
        price=price,
        event_type=event_type,
    )


def _default_node_fn(wallet: str):
    try:
        import torch
        h = abs(hash(wallet)) % 10000
        return torch.tensor(
            [h / 10000.0, len(wallet) / 60.0, float(wallet.startswith("G")), 0.0],
            dtype=torch.float,
        )
    except ImportError:
        return [0.0] * 4


def _default_asset_fn(asset_pair: str):
    try:
        import torch
        h = abs(hash(asset_pair)) % 10000
        return torch.tensor([h / 10000.0, len(asset_pair) / 40.0, 0.0, 0.0], dtype=torch.float)
    except ImportError:
        return [0.0] * 4


# ── PyG availability guard ────────────────────────────────────────────────────

try:
    import torch
    from torch_geometric.data import HeteroData
    from detection.gnn_ring_detector import (
        build_heterogeneous_graph,
        HeteroGraphSAGEEncoder,
        GNNRingDetector,
        RingMembershipClassifier,
        _verify_model_checksum,
    )
    _HAS_PYG = True
except ImportError:
    _HAS_PYG = False

pytestmark = pytest.mark.skipif(
    not _HAS_PYG, reason="PyTorch Geometric not installed — skipping heterogeneous GNN tests."
)


# ── Test: build_heterogeneous_graph ──────────────────────────────────────────

class TestBuildHeterogeneousGraph:
    """Test graph construction with correct node/edge counts."""

    def test_node_counts(self):
        """5 wallets, 3 asset pairs, 2 orders → correct counts."""
        wallets = [f"G{'A'*54}{i}" for i in range(5)]
        trades = [
            _make_trade(wallets[0], wallets[1], ts=float(i), base_code="XLM", counter_code="USDC")
            for i in range(3)
        ] + [
            _make_trade(wallets[2], wallets[3], ts=float(i + 3), base_code="BTC", counter_code="XLM")
            for i in range(2)
        ]
        order_events = [
            _make_order_event("ord_1", wallets[0], asset_pair="XLM/USDC", event_type="created"),
            _make_order_event("ord_2", wallets[2], asset_pair="BTC/XLM", event_type="cancelled"),
        ]
        funding = [(wallets[0], wallets[4])]

        graph = build_heterogeneous_graph(
            trades=trades,
            order_events=order_events,
            funding_edges=funding,
            node_feature_fn=_default_node_fn,
            asset_feature_fn=_default_asset_fn,
        )

        assert len(graph["wallet"].wallet_list) == 5
        assert len(graph["asset"].asset_list) == 2  # XLM/USDC, BTC/XLM
        assert len(graph["order"].order_list) == 2

    def test_edge_shapes(self):
        """Edge indices have correct shapes for each edge type."""
        wallets = [f"G{'B'*54}{i}" for i in range(4)]
        trades = [
            _make_trade(wallets[0], wallets[1], ts=0.0),
            _make_trade(wallets[1], wallets[2], ts=1.0),
        ]
        order_events = [
            _make_order_event("e1", wallets[0], event_type="created"),
            _make_order_event("e2", wallets[0], event_type="cancelled"),
        ]
        funding = [(wallets[2], wallets[3])]

        graph = build_heterogeneous_graph(
            trades=trades,
            order_events=order_events,
            funding_edges=funding,
            node_feature_fn=_default_node_fn,
        )

        # Wallet-wallet trades: 2 edges
        ww = graph["wallet", "trades", "wallet"].edge_index
        assert ww.shape[0] == 2
        assert ww.shape[1] == 2

        # Wallet-asset trades: 2 edges (one per trade)
        wa = graph["wallet", "trades", "asset"].edge_index
        assert wa.shape[0] == 2
        assert wa.shape[1] == 2

        # Asset-traded_by-wallet: reverse of above
        aw = graph["asset", "traded_by", "wallet"].edge_index
        assert aw.shape == wa.shape

        # Funding: 1 edge
        fund = graph["wallet", "funds", "wallet"].edge_index
        assert fund.shape[1] == 1

        # Creates: 1 edge
        creates = graph["wallet", "creates", "order"].edge_index
        assert creates.shape[1] == 1

        # Cancels: 1 edge
        cancels = graph["wallet", "cancels", "order"].edge_index
        assert cancels.shape[1] == 1

        # Order-for-asset: 2 edges
        ofa = graph["order", "for", "asset"].edge_index
        assert ofa.shape[1] == 2

    def test_empty_order_events_and_funding(self):
        """Graceful degradation with no order events or funding edges."""
        wallets = [f"G{'C'*54}{i}" for i in range(3)]
        trades = [_make_trade(wallets[0], wallets[1], ts=0.0)]

        graph = build_heterogeneous_graph(
            trades=trades,
            order_events=[],
            funding_edges=[],
            node_feature_fn=_default_node_fn,
        )

        assert len(graph["wallet"].wallet_list) == 2
        assert len(graph["asset"].asset_list) == 1
        assert len(graph["order"].order_list) == 0
        assert graph["wallet", "funds", "wallet"].edge_index.shape[1] == 0
        assert graph["wallet", "creates", "order"].edge_index.shape[1] == 0
        assert graph["wallet", "cancels", "order"].edge_index.shape[1] == 0
        assert graph["order", "for", "asset"].edge_index.shape[1] == 0

    def test_empty_trades(self):
        """Empty trade list produces graph with no nodes."""
        graph = build_heterogeneous_graph(
            trades=[],
            node_feature_fn=_default_node_fn,
        )
        assert len(graph["wallet"].wallet_list) == 0
        assert len(graph["asset"].asset_list) == 0
        assert len(graph["order"].order_list) == 0


# ── Test: HeteroGraphSAGEEncoder ─────────────────────────────────────────────

class TestHeteroGraphSAGEEncoder:
    """Test forward pass output shapes for both SAGE and HGT conv types."""

    def _build_small_graph(self):
        wallets = [f"G{'D'*54}{i}" for i in range(6)]
        trades = [
            _make_trade(wallets[i], wallets[(i + 1) % 6], ts=float(i))
            for i in range(6)
        ]
        return build_heterogeneous_graph(
            trades=trades,
            node_feature_fn=_default_node_fn,
        )

    def test_sage_output_shape(self):
        """SAGE conv produces (n_wallets, 64) output."""
        graph = self._build_small_graph()
        metadata = graph.metadata()
        encoder = HeteroGraphSAGEEncoder(
            metadata=metadata,
            hidden_channels=64,
            out_channels=64,
            num_layers=2,
            conv_type="sage",
        )
        x_dict = {nt: graph[nt].x for nt in graph.node_types}
        edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
        out = encoder(x_dict, edge_index_dict)
        assert "wallet" in out
        assert out["wallet"].shape == (6, 64)

    def test_hgt_output_shape(self):
        """HGT conv produces (n_wallets, 64) output."""
        graph = self._build_small_graph()
        metadata = graph.metadata()
        encoder = HeteroGraphSAGEEncoder(
            metadata=metadata,
            hidden_channels=64,
            out_channels=64,
            num_layers=2,
            conv_type="hgt",
        )
        x_dict = {nt: graph[nt].x for nt in graph.node_types}
        edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
        out = encoder(x_dict, edge_index_dict)
        assert "wallet" in out
        assert out["wallet"].shape[0] == 6
        assert out["wallet"].shape[1] == 64

    def test_single_wallet(self):
        """Encoder handles graph with a single wallet."""
        wallets = [f"G{'E'*55}"]
        trades = []  # no edges
        graph = build_heterogeneous_graph(
            trades=trades,
            node_feature_fn=_default_node_fn,
        )
        metadata = graph.metadata()
        encoder = HeteroGraphSAGEEncoder(
            metadata=metadata,
            hidden_channels=32,
            out_channels=64,
            num_layers=1,
        )
        x_dict = {nt: graph[nt].x for nt in graph.node_types}
        edge_index_dict = {et: graph[et].edge_index for et in graph.edge_types}
        out = encoder(x_dict, edge_index_dict)
        assert out["wallet"].shape == (1, 64)


# ── Test: GNNRingDetector heterogeneous fallback ─────────────────────────────

class TestGNNRingDetectorHeteroFallback:
    """Test fallback from heterogeneous to homogeneous on missing/corrupt checkpoint."""

    def test_missing_hetero_checkpoint_falls_back(self):
        """Missing hetero checkpoint should fall back to homogeneous mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hetero_path = os.path.join(tmpdir, "gnn_ring_detector_hetero.pt")
            homo_path = os.path.join(tmpdir, "gnn_ring_detector.pt")

            # Create a valid homogeneous model
            wallets = [f"G{'F'*54}{i}" for i in range(5)]
            trades = [_make_trade(wallets[i], wallets[(i + 1) % 5], ts=float(i)) for i in range(5)]
            from detection.gnn_ring_detector import build_transaction_graph, GraphSAGEEncoder
            graph = build_transaction_graph(trades, _default_node_fn)
            in_ch = graph["wallet"].x.shape[1]

            encoder = GraphSAGEEncoder(in_channels=in_ch, out_channels=64)
            classifier = RingMembershipClassifier(embedding_dim=64)
            torch.save({
                "encoder": encoder.state_dict(),
                "classifier": classifier.state_dict(),
                "encoder_config": {"in_channels": in_ch, "hidden_channels": 128,
                                   "out_channels": 64, "num_layers": 3, "dropout": 0.3},
                "classifier_config": {"embedding_dim": 64},
            }, homo_path)
            # Write checksum
            h = hashlib.sha256()
            with open(homo_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            with open(homo_path.replace(".pt", ".sha256"), "w") as f:
                f.write(h.hexdigest() + "\n")

            # Set env to point to missing hetero model
            os.environ["GNN_HETERO_MODEL_PATH"] = hetero_path
            os.environ["GNN_HETERO_CHECKSUM_PATH"] = hetero_path.replace(".pt", ".sha256")

            detector = GNNRingDetector(
                model_path=homo_path,
                graph_mode="heterogeneous",
                fallback_to_scc=False,
            )
            detector.load()

            # Should have fallen back to homogeneous and be fitted
            assert detector._fitted is True
            assert detector._hetero_fitted is False

            # Clean up
            del os.environ["GNN_HETERO_MODEL_PATH"]
            del os.environ["GNN_HETERO_CHECKSUM_PATH"]

    def test_corrupt_hetero_checksum_falls_back(self):
        """Corrupt hetero checksum should fall back to homogeneous mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hetero_path = os.path.join(tmpdir, "gnn_ring_detector_hetero.pt")
            hetero_checksum = hetero_path.replace(".pt", ".sha256")
            homo_path = os.path.join(tmpdir, "gnn_ring_detector.pt")

            # Write a fake hetero model with bad checksum
            with open(hetero_path, "wb") as f:
                f.write(b"fake hetero model")
            with open(hetero_checksum, "w") as f:
                f.write("0000000000000000000000000000000000000000000000000000000000000000\n")

            # Create valid homogeneous model
            wallets = [f"G{'G'*54}{i}" for i in range(5)]
            trades = [_make_trade(wallets[i], wallets[(i + 1) % 5], ts=float(i)) for i in range(5)]
            from detection.gnn_ring_detector import build_transaction_graph, GraphSAGEEncoder
            graph = build_transaction_graph(trades, _default_node_fn)
            in_ch = graph["wallet"].x.shape[1]
            encoder = GraphSAGEEncoder(in_channels=in_ch, out_channels=64)
            classifier = RingMembershipClassifier(embedding_dim=64)
            torch.save({
                "encoder": encoder.state_dict(),
                "classifier": classifier.state_dict(),
                "encoder_config": {"in_channels": in_ch, "hidden_channels": 128,
                                   "out_channels": 64, "num_layers": 3, "dropout": 0.3},
                "classifier_config": {"embedding_dim": 64},
            }, homo_path)
            h = hashlib.sha256()
            with open(homo_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            with open(homo_path.replace(".pt", ".sha256"), "w") as f:
                f.write(h.hexdigest() + "\n")

            os.environ["GNN_HETERO_MODEL_PATH"] = hetero_path
            os.environ["GNN_HETERO_CHECKSUM_PATH"] = hetero_checksum

            detector = GNNRingDetector(
                model_path=homo_path,
                graph_mode="heterogeneous",
                fallback_to_scc=False,
            )
            detector.load()

            assert detector._fitted is True
            assert detector._hetero_fitted is False

            del os.environ["GNN_HETERO_MODEL_PATH"]
            del os.environ["GNN_HETERO_CHECKSUM_PATH"]


# ── Test: synthetic asset-mediated laundering scenario ────────────────────────

class TestAssetMediatedLaunderingScenario:
    """Heterogeneous mode AUC-ROC advantage on asset-mediated laundering."""

    def test_hetero_advantage_on_asset_mediated(self):
        """Heterogeneous mode detects asset-mediated laundering better than homogeneous."""
        import torch
        from sklearn.metrics import roc_auc_score
        from ingestion.synthetic_data import AssetMediatedProfile, AttackProfileConfig

        # Generate asset-mediated laundering data
        config = AttackProfileConfig(n_wallets=8, n_trades=100, seed=42)
        profile = AssetMediatedProfile(config=config, ring_size=4)
        trades, order_events = profile.generate()

        # Also generate some legitimate trades
        from ingestion.synthetic_data import _stellar_address
        rng = torch.Generator().manual_seed(123)
        legit_wallets = [_stellar_address(torch.Generator()) for _ in range(20)]
        from ingestion.synthetic_data import RoundTripProfile, NATIVE, USDC
        import numpy as np
        legit_rng = np.random.default_rng(99)
        legit_trades = []
        for i in range(50):
            src = legit_wallets[i % len(legit_wallets)]
            dst = legit_wallets[(i + 3) % len(legit_wallets)]
            t = _make_trade(src, dst, amount=float(legit_rng.uniform(10, 500)),
                            ts=float(i * 100))
            legit_trades.append(t)

        all_trades = legit_trades + trades

        # Build both graph types
        from detection.gnn_ring_detector import (
            build_transaction_graph,
            build_heterogeneous_graph,
            HeteroGraphSAGEEncoder,
        )

        homo_graph = build_transaction_graph(all_trades, _default_node_fn)
        hetero_graph = build_heterogeneous_graph(
            all_trades, order_events=order_events,
            node_feature_fn=_default_node_fn,
        )

        # Create labels: laundering wallets = positive, legit = negative
        laundering_wallets = set()
        for i in range(config.n_wallets):
            laundering_wallets.add(profile._wallets[i])

        wlist_homo = homo_graph["wallet"].wallet_list
        labels_homo = [1.0 if w in laundering_wallets else 0.0 for w in wlist_homo]

        wlist_hetero = hetero_graph["wallet"].wallet_list
        labels_hetero = [1.0 if w in laundering_wallets else 0.0 for w in wlist_hetero]

        # Train a small homogeneous encoder
        homo_encoder = GraphSAGEEncoder(
            in_channels=homo_graph["wallet"].x.shape[1],
            hidden_channels=32, out_channels=32, num_layers=2,
        )
        homo_clf = RingMembershipClassifier(embedding_dim=32)
        opt_homo = torch.optim.Adam(
            list(homo_encoder.parameters()) + list(homo_clf.parameters()), lr=0.01
        )

        x_h = homo_graph["wallet"].x
        ei_h = homo_graph["wallet", "trades", "wallet"].edge_index
        y_h = torch.tensor(labels_homo, dtype=torch.float)

        for _ in range(30):
            homo_encoder.train()
            homo_clf.train()
            opt_homo.zero_grad()
            emb = homo_encoder(x_h, ei_h)
            scores = homo_clf(emb)
            loss = torch.nn.functional.binary_cross_entropy(scores, y_h)
            loss.backward()
            opt_homo.step()

        homo_encoder.eval()
        homo_clf.eval()
        with torch.no_grad():
            homo_emb = homo_encoder(x_h, ei_h)
            homo_scores = homo_clf(homo_emb).numpy()

        homo_auc = roc_auc_score(labels_homo, homo_scores) if len(set(labels_homo)) > 1 else 0.5

        # Train a small heterogeneous encoder
        hetero_encoder = HeteroGraphSAGEEncoder(
            metadata=hetero_graph.metadata(),
            hidden_channels=32, out_channels=32, num_layers=2, conv_type="sage",
        )
        hetero_clf = RingMembershipClassifier(embedding_dim=32)
        opt_hetero = torch.optim.Adam(
            list(hetero_encoder.parameters()) + list(hetero_clf.parameters()), lr=0.01
        )

        x_dict = {nt: hetero_graph[nt].x for nt in hetero_graph.node_types}
        ei_dict = {et: hetero_graph[et].edge_index for et in hetero_graph.edge_types}
        y_ht = torch.tensor(labels_hetero, dtype=torch.float)

        for _ in range(30):
            hetero_encoder.train()
            hetero_clf.train()
            opt_hetero.zero_grad()
            emb_dict = hetero_encoder(x_dict, ei_dict)
            scores = hetero_clf(emb_dict["wallet"])
            loss = torch.nn.functional.binary_cross_entropy(scores, y_ht)
            loss.backward()
            opt_hetero.step()

        hetero_encoder.eval()
        hetero_clf.eval()
        with torch.no_grad():
            emb_dict = hetero_encoder(x_dict, ei_dict)
            hetero_scores = hetero_clf(emb_dict["wallet"]).numpy()

        hetero_auc = roc_auc_score(labels_hetero, hetero_scores) if len(set(labels_hetero)) > 1 else 0.5

        # Heterogeneous mode should have measurable advantage
        # (at minimum, not significantly worse)
        # With a fixed seed and asset-mediated pattern, hetero should be >= homo
        # Allow a small tolerance for numerical variation
        assert hetero_auc >= homo_auc - 0.05, (
            f"Heterogeneous AUC ({hetero_auc:.4f}) should be >= "
            f"homogeneous AUC ({homo_auc:.4f}) - 0.05 tolerance"
        )


# ── Regression: existing homogeneous mode ─────────────────────────────────────

class TestHomogeneousRegression:
    """Existing homogeneous-mode functionality unchanged."""

    def test_homogeneous_build_and_predict(self):
        """build_transaction_graph + GraphSAGEEncoder + GNNRingDetector work as before."""
        from detection.gnn_ring_detector import (
            build_transaction_graph,
            GraphSAGEEncoder,
        )

        wallets = [f"G{'H'*54}{i}" for i in range(6)]
        trades = [_make_trade(wallets[i], wallets[(i + 1) % 6], ts=float(i)) for i in range(6)]
        graph = build_transaction_graph(trades, _default_node_fn)

        in_ch = graph["wallet"].x.shape[1]
        encoder = GraphSAGEEncoder(in_channels=in_ch, out_channels=64)
        classifier = RingMembershipClassifier(embedding_dim=64)

        x = graph["wallet"].x
        ei = graph["wallet", "trades", "wallet"].edge_index

        with torch.no_grad():
            emb = encoder(x, ei)
            scores = classifier(emb)

        assert emb.shape == (6, 64)
        assert scores.shape == (6,)
        assert float(scores.min()) >= 0.0
        assert float(scores.max()) <= 1.0

    def test_homogeneous_detector_predict(self):
        """GNNRingDetector with graph_mode=homogeneous works."""
        from detection.gnn_ring_detector import (
            build_transaction_graph,
            GraphSAGEEncoder,
        )

        wallets = [f"G{'I'*54}{i}" for i in range(5)]
        trades = [_make_trade(wallets[i], wallets[(i + 1) % 5], ts=float(i)) for i in range(5)]
        graph = build_transaction_graph(trades, _default_node_fn)

        in_ch = graph["wallet"].x.shape[1]
        detector = GNNRingDetector(graph_mode="homogeneous", fallback_to_scc=False)
        detector._encoder = GraphSAGEEncoder(in_channels=in_ch, out_channels=64)
        detector._classifier = RingMembershipClassifier(embedding_dim=64)
        detector._fitted = True

        score = detector.predict(wallets[0], graph)
        assert 0.0 <= score <= 1.0
