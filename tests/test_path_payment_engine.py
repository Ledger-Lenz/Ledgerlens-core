"""Tests for PathPaymentGraph and PathCycleDetector (issue #121)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from detection.path_payment_engine import (
    HopEdge,
    MAX_EDGES_PER_WALLET,
    PathCycleDetector,
    PathPaymentCycle,
    PathPaymentGraph,
    _score_cycle,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_W = "G" + "A" * 55  # valid Stellar address template
_W2 = "G" + "B" * 55
_W3 = "G" + "C" * 55
_W4 = "G" + "D" * 55
_W5 = "G" + "E" * 55
_W6 = "G" + "F" * 55
_W7 = "G" + "G" * 55

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _hop(
    src_wallet: str,
    src_asset: str,
    dst_wallet: str,
    dst_asset: str,
    amount: float,
    seconds_offset: int,
    op_id: str,
) -> HopEdge:
    return HopEdge(
        src_wallet=src_wallet,
        src_asset=src_asset,
        dst_wallet=dst_wallet,
        dst_asset=dst_asset,
        amount=amount,
        ledger_timestamp=_BASE + timedelta(seconds=seconds_offset),
        operation_id=op_id,
    )


def _make_3hop_cycle(recovery: float = 1.0, duration_s: int = 60) -> list[HopEdge]:
    """W -> W2 -> W3 -> W with given recovery ratio and duration."""
    return [
        _hop(_W, "XLM", _W2, "USDC", 100.0, 0, "op1"),
        _hop(_W2, "USDC", _W3, "BTC", 100.0, duration_s // 2, "op2"),
        _hop(_W3, "BTC", _W, "XLM", 100.0 * recovery, duration_s, "op3"),
    ]


def _make_7hop_cycle(recovery: float = 0.96, duration_s: int = 3500) -> list[HopEdge]:
    wallets = [_W, _W2, _W3, _W4, _W5, _W6, _W7]
    assets = ["XLM", "USDC", "BTC", "ETH", "AQUA", "yXLM", "XLM"]
    step = duration_s // 7
    hops = []
    for i in range(6):
        hops.append(
            _hop(wallets[i], assets[i], wallets[i + 1], assets[i + 1], 100.0, step * i, f"op7_{i}")
        )
    hops.append(
        _hop(wallets[6], assets[6], wallets[0], "XLM", 100.0 * recovery, duration_s, "op7_6")
    )
    return hops


# ---------------------------------------------------------------------------
# PathPaymentGraph tests
# ---------------------------------------------------------------------------


def test_3hop_cycle_detected_with_high_score():
    g = PathPaymentGraph()
    for h in _make_3hop_cycle(recovery=1.0, duration_s=60):
        g.add_hop(h)
    cycles = g.find_cycles(_W)
    assert len(cycles) >= 1
    c = max(cycles, key=lambda x: x.cycle_score)
    assert c.recovery_ratio == pytest.approx(1.0, abs=0.01)
    assert c.cycle_score > 0.8


def test_7hop_cycle_detected_with_minimum_score():
    g = PathPaymentGraph()
    for h in _make_7hop_cycle(recovery=0.96, duration_s=3500):
        g.add_hop(h)
    cycles = g.find_cycles(_W)
    assert len(cycles) >= 1
    c = max(cycles, key=lambda x: x.cycle_score)
    # Formula: 0.40*0.96 + 0.30*(1/(1+3500/600)) + 0.20*(6/7) + 0.10*0 ≈ 0.599
    assert c.cycle_score > 0.58


def test_partial_recovery_below_threshold_not_returned():
    g = PathPaymentGraph(cycle_window_seconds=3600.0)
    hops = _make_3hop_cycle(recovery=0.4, duration_s=60)
    for h in hops:
        g.add_hop(h)
    cycles = g.find_cycles(_W)
    # recovery_ratio of 0.4 is below the 0.5 minimum in find_cycles
    assert all(c.recovery_ratio >= 0.5 for c in cycles)


def test_out_of_order_hops_still_form_cycle():
    hops = _make_3hop_cycle(recovery=1.0, duration_s=60)
    # Shuffle the ingestion order
    reordered = [hops[2], hops[0], hops[1]]
    g = PathPaymentGraph()
    for h in reordered:
        g.add_hop(h)
    cycles = g.find_cycles(_W)
    assert len(cycles) >= 1


def test_duplicate_operation_id_does_not_create_duplicate():
    g = PathPaymentGraph()
    h = _hop(_W, "XLM", _W2, "USDC", 100.0, 0, "dup_op")
    g.add_hop(h)
    g.add_hop(h)  # duplicate
    g.add_hop(h)  # again
    # Only one edge should exist for this src->dst pair
    node = (_W, "XLM")
    dst = (_W2, "USDC")
    edge_list = g._adj.get(node, {}).get(dst, [])
    assert len(edge_list) == 1


def test_max_edges_per_wallet_guard_logs_warning(caplog):
    g = PathPaymentGraph()
    w_src = "G" + "A" * 55
    w_dst = "G" + "B" * 55
    with caplog.at_level(logging.WARNING):
        for i in range(MAX_EDGES_PER_WALLET + 10):
            g.add_hop(
                HopEdge(
                    src_wallet=w_src,
                    src_asset=f"ASSET{i % 12:04d}"[:12],
                    dst_wallet=w_dst,
                    dst_asset="XLM",
                    amount=1.0,
                    ledger_timestamp=_BASE + timedelta(seconds=i),
                    operation_id=f"op_cap_{i}",
                )
            )
    assert any("MAX_EDGES_PER_WALLET" in r.message for r in caplog.records)


def test_get_features_zeros_for_unknown_wallet():
    d = PathCycleDetector()
    features = d.get_features("G" + "Z" * 55)
    assert features == {"path_cycle_count": 0.0, "path_cycle_recovery_ratio": 0.0}


# ---------------------------------------------------------------------------
# PathCycleDetector tests
# ---------------------------------------------------------------------------


def _hop_to_record(h: HopEdge) -> dict:
    return {
        "source_account": h.src_wallet,
        "to": h.dst_wallet,
        "asset_code": h.src_asset,
        "destination_asset_code": h.dst_asset,
        "amount": h.amount,
        "created_at": h.ledger_timestamp.isoformat(),
        "id": h.operation_id,
    }


def test_detector_ingest_3hop_cycle_emits_confirmed_cycle():
    d = PathCycleDetector(min_recovery_ratio=0.95, min_cycle_score=0.6)
    records = [_hop_to_record(h) for h in _make_3hop_cycle(recovery=1.0, duration_s=60)]
    cycles = d.ingest(records)
    assert len(cycles) >= 1
    assert all(c.cycle_score >= 0.6 for c in cycles)
    assert all(c.recovery_ratio >= 0.95 for c in cycles)


def test_detector_partial_recovery_not_attributed_to_origin():
    """Cycle where the origin wallet recovers only 50% is not attributed to it."""
    d = PathCycleDetector(min_recovery_ratio=0.95, min_cycle_score=0.0)
    records = [_hop_to_record(h) for h in _make_3hop_cycle(recovery=0.5, duration_s=60)]
    d.ingest(records)
    # _W only recovers 50% so no cycle should be attributed to _W
    features = d.get_features(_W)
    assert features["path_cycle_count"] == 0.0


def test_detector_get_features_after_cycle():
    d = PathCycleDetector(min_recovery_ratio=0.95, min_cycle_score=0.0)
    records = [_hop_to_record(h) for h in _make_3hop_cycle(recovery=1.0, duration_s=60)]
    d.ingest(records)
    features = d.get_features(_W)
    assert features["path_cycle_count"] >= 1.0
    assert features["path_cycle_recovery_ratio"] == pytest.approx(1.0, abs=0.01)


def test_detector_invalid_window_raises():
    with pytest.raises(ValueError):
        PathCycleDetector(cycle_window_seconds=100)  # below 300


def test_detector_7hop_cycle_score():
    d = PathCycleDetector(min_recovery_ratio=0.95, min_cycle_score=0.0)
    records = [_hop_to_record(h) for h in _make_7hop_cycle(recovery=0.96, duration_s=3500)]
    cycles = d.ingest(records)
    assert len(cycles) >= 1
    best = max(cycles, key=lambda c: c.cycle_score)
    assert best.cycle_score > 0.6


# ---------------------------------------------------------------------------
# API integration test
# ---------------------------------------------------------------------------


def test_api_path_cycles_endpoint(tmp_path):
    """GET /path-cycles endpoint returns correct schema."""
    from dataclasses import replace
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from api.main import app
    from config.settings import settings
    from detection.storage import init_db, save_hop_payment_cycles

    db = str(tmp_path / "test.db")
    test_settings = replace(settings, db_path=db)

    init_db(db)
    cycle = PathPaymentCycle(
        origin_wallet=_W,
        origin_asset="XLM",
        hops=_make_3hop_cycle(recovery=1.0, duration_s=60),
        recovery_ratio=1.0,
        cycle_duration_seconds=60.0,
        counterparty_overlap=0.0,
        cycle_score=0.85,
    )
    save_hop_payment_cycles([cycle], db_path=db)

    with patch("detection.storage.settings", test_settings):
        client = TestClient(app)
        resp = client.get("/path-cycles?min_score=0.6")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["origin_wallet"] == _W
    assert data[0]["cycle_score"] >= 0.6
