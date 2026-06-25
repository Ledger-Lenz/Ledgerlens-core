"""Tests for issue #152: Stateful Rolling-Window Streaming Scorer.

Covers:
- WalletWindow eviction and memory cap
- RollingWindowState add/get
- RollingWindowStore save/load round-trip
- IncrementalScorer delta suppression and triggering
- GET /stream/status endpoint
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from ingestion.data_models import Asset, Trade, TradeType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trade(base_account: str, ts: datetime, idx: int = 0) -> Trade:
    return Trade(
        id=f"t-{idx}",
        ledger_close_time=ts,
        base_account=base_account,
        counter_account="GCOUNTER",
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"),
        base_amount=100.0,
        counter_amount=200.0,
        price=2.0,
        base_is_seller=True,
    )


_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# WalletWindow tests
# ---------------------------------------------------------------------------

class TestWalletWindow:
    def test_add_and_get_within_window(self):
        from detection.rolling_window import WalletWindow
        ww = WalletWindow()
        t = _trade("GABC", _NOW - timedelta(minutes=30))
        ww.add(t)
        assert len(ww.get(1)) == 1

    def test_get_excludes_older_trades(self):
        from detection.rolling_window import WalletWindow
        ww = WalletWindow()
        # 5 trades in last hour, 5 older
        for i in range(5):
            ww._trades.append(_trade("GABC", _NOW - timedelta(minutes=30 + i), i))
        for i in range(5):
            ww._trades.append(_trade("GABC", _NOW - timedelta(hours=2 + i), 10 + i))
        result = ww.get(1)
        assert len(result) == 5

    def test_eviction_removes_old_trades(self):
        from detection.rolling_window import WalletWindow
        ww = WalletWindow()
        # Add a trade that is 25h old — should be evicted on next add
        old = _trade("GABC", _NOW - timedelta(hours=25), 0)
        ww._trades.append(old)
        fresh = _trade("GABC", _NOW, 1)
        ww.add(fresh)
        assert len(ww._trades) == 1
        assert ww._trades[0].id == "t-1"

    def test_memory_cap(self):
        from detection.rolling_window import MAX_TRADES_PER_WALLET_WINDOW, WalletWindow
        ww = WalletWindow()
        # Fill exactly at cap via add() so eviction + cap logic both run
        for i in range(MAX_TRADES_PER_WALLET_WINDOW + 1):
            # all trades within 24h so no natural eviction
            ww.add(_trade("GABC", _NOW - timedelta(seconds=i), i))
        assert len(ww._trades) == MAX_TRADES_PER_WALLET_WINDOW


# ---------------------------------------------------------------------------
# RollingWindowState tests
# ---------------------------------------------------------------------------

class TestRollingWindowState:
    def test_add_trade_creates_wallet(self):
        from detection.rolling_window import RollingWindowState
        state = RollingWindowState()
        state.add_trade("GABC", _trade("GABC", _NOW))
        assert state.active_wallets == 1

    def test_get_window_empty_wallet(self):
        from detection.rolling_window import RollingWindowState
        state = RollingWindowState()
        assert state.get_window("GNONE", 1) == []

    def test_get_window_scoped(self):
        from detection.rolling_window import RollingWindowState
        state = RollingWindowState()
        for i in range(3):
            state.add_trade("GABC", _trade("GABC", _NOW - timedelta(minutes=10 + i), i))
        for i in range(3):
            state.add_trade("GABC", _trade("GABC", _NOW - timedelta(hours=5 + i), 10 + i))
        # Only 3 within 1h
        assert len(state.get_window("GABC", 1)) == 3
        # All 6 within 24h
        assert len(state.get_window("GABC", 24)) == 6


# ---------------------------------------------------------------------------
# RollingWindowStore tests
# ---------------------------------------------------------------------------

class TestRollingWindowStore:
    def test_save_and_load_round_trip(self):
        from detection.rolling_window import RollingWindowState, RollingWindowStore
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = RollingWindowStore(db_path=f.name)
            state = RollingWindowState()
            wallets = ["GABC", "GDEF", "GHIJ"]
            for w in wallets:
                for i in range(3):
                    state.add_trade(w, _trade(w, _NOW - timedelta(minutes=i), i))
            store.save_all(state)

            # Load into a fresh state
            state2 = RollingWindowState()
            store.load_all(state2)
            assert state2.active_wallets == 3
            for w in wallets:
                assert len(state2.get_window(w, 24)) == 3

    def test_load_state_missing_wallet(self):
        from detection.rolling_window import RollingWindowStore
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = RollingWindowStore(db_path=f.name)
            result = store.load_state("GNOBODY")
            assert result is None


# ---------------------------------------------------------------------------
# IncrementalScorer tests
# ---------------------------------------------------------------------------

def _make_scorer(score_sequence: list[int], delta: int = 5):
    """Build an IncrementalScorer whose model returns scores from score_sequence in order."""
    from detection.feature_engineering import FeatureEngineering
    from detection.model_inference import IncrementalScorer, ModelInference
    from detection.rolling_window import RollingWindowState

    state = RollingWindowState()
    fe = FeatureEngineering()

    scores_iter = iter(score_sequence)

    mock_model_inference = MagicMock(spec=ModelInference)
    from detection.risk_score import RiskScore
    def _score(wallet, asset_pair, features):
        s = next(scores_iter)
        return RiskScore(
            wallet=wallet,
            asset_pair=asset_pair,
            score=s,
            benford_flag=False,
            ml_flag=s >= 50,
            confidence=80,
            timestamp=datetime.now(timezone.utc),
        )
    mock_model_inference.score.side_effect = _score

    # Stub out compute_incremental to avoid needing real feature computation
    fe.compute_incremental = MagicMock(return_value={})

    scorer = IncrementalScorer(
        window_state=state,
        feature_engineering=fe,
        model_inference=mock_model_inference,
        score_delta_threshold=delta,
    )
    return scorer


class TestIncrementalScorer:
    def test_first_trade_always_emits(self):
        scorer = _make_scorer([50])
        trade = _trade("GABC", _NOW)
        result = scorer.score_on_trade(trade)
        assert result is not None
        assert result.score == 50

    def test_delta_suppression(self):
        """Scores 82 then 83 — delta=1 < threshold=5 → second call returns None."""
        scorer = _make_scorer([82, 83], delta=5)
        t1 = _trade("GABC", _NOW, 0)
        t2 = _trade("GABC", _NOW, 1)
        r1 = scorer.score_on_trade(t1)
        r2 = scorer.score_on_trade(t2)
        assert r1 is not None
        assert r1.score == 82
        assert r2 is None

    def test_delta_trigger(self):
        """Scores 70 then 76 — delta=6 >= 5 → second call returns RiskScore."""
        scorer = _make_scorer([70, 76], delta=5)
        t1 = _trade("GABC", _NOW, 0)
        t2 = _trade("GABC", _NOW, 1)
        r1 = scorer.score_on_trade(t1)
        r2 = scorer.score_on_trade(t2)
        assert r1 is not None
        assert r2 is not None
        assert r2.score == 76

    def test_multiple_wallets_independent(self):
        """Delta tracking is per-wallet."""
        from detection.feature_engineering import FeatureEngineering
        from detection.model_inference import IncrementalScorer, ModelInference
        from detection.rolling_window import RollingWindowState
        from detection.risk_score import RiskScore

        state = RollingWindowState()
        fe = FeatureEngineering()
        fe.compute_incremental = MagicMock(return_value={})

        scores = {"GABC": iter([50, 52]), "GDEF": iter([60, 66])}

        mock_mi = MagicMock(spec=ModelInference)
        def _score(wallet, asset_pair, features):
            s = next(scores[wallet])
            return RiskScore(wallet=wallet, asset_pair=asset_pair, score=s,
                             benford_flag=False, ml_flag=False, confidence=80,
                             timestamp=datetime.now(timezone.utc))
        mock_mi.score.side_effect = _score

        scorer = IncrementalScorer(state, fe, mock_mi, score_delta_threshold=5)

        # GABC: 50 → emit; GDEF: 60 → emit
        assert scorer.score_on_trade(_trade("GABC", _NOW, 0)) is not None
        assert scorer.score_on_trade(_trade("GDEF", _NOW, 1)) is not None
        # GABC: 52 → suppress (delta=2); GDEF: 66 → emit (delta=6)
        assert scorer.score_on_trade(_trade("GABC", _NOW, 2)) is None
        assert scorer.score_on_trade(_trade("GDEF", _NOW, 3)) is not None


# ---------------------------------------------------------------------------
# GET /stream/status tests
# ---------------------------------------------------------------------------

class TestStreamStatus:
    def test_stream_status_returns_required_fields(self):
        from fastapi.testclient import TestClient
        from api.main import app
        client = TestClient(app)
        resp = client.get("/stream/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "trades_per_second" in body
        assert "active_wallets" in body
        assert "last_trade_at" in body

    def test_stream_status_updates_after_trade(self):
        import api.main as api_main
        from fastapi.testclient import TestClient
        from api.main import app

        trade = _trade("GABC", datetime.now(timezone.utc))
        api_main._stream_status_update(trade)

        client = TestClient(app)
        resp = client.get("/stream/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_trade_at"] is not None
        assert body["trades_per_second"] >= 0
