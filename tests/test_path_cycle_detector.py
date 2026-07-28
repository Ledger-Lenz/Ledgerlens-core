import time
import uuid

import pandas as pd
import pytest

from detection.feature_engineering import (
    PATH_PAYMENT_CYCLE_FEATURE_NAMES,
    path_payment_cycle_features,
)
from detection.path_cycle_detector import (
    build_path_payment_graph,
    detect_cycles_from_payments,
    detect_path_payment_cycles,
    path_cycle_features,
    path_payment_cycles_to_alerts,
)
from detection.storage import AlertType, get_alerts, save_alerts
from ingestion.data_models import Asset, PathPayment


class TestCleanupLockIn:
    """Lock in the cleanup changes so future edits don't regress."""

    def test_heapq_is_module_level_not_local_import(self):
        """`heapq` must be at module top level, not inside a function."""
        import ast
        import detection.path_cycle_detector as m
        src = ast.parse(m.__loader__.get_source(m.__name__))
        module_imports: set[str] = set()
        for node in ast.iter_child_nodes(src):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_imports.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    module_imports.add(alias.asname or alias.name)
        assert "heapq" in module_imports, (
            "heapq must be imported at module level, not inside _select_timed_cycle_edges"
        )

    def test_alerttype_is_module_level_not_local_import(self):
        """`AlertType` must be at module top level, not inside a function."""
        import ast
        import detection.path_cycle_detector as m
        src = ast.parse(m.__loader__.get_source(m.__name__))
        for node in ast.iter_child_nodes(src):
            if isinstance(node, ast.ImportFrom):
                imported = {a.name for a in node.names}
                if "AlertType" in imported:
                    return  # found at top level
        pytest.fail("AlertType must be imported at module level, not inside path_payment_cycles_to_alerts")

    def test_module_uses_future_annotations(self):
        """Lock in the `from __future__ import annotations` cleanup."""
        import ast
        import detection.path_cycle_detector as m
        src = ast.parse(m.__loader__.get_source(m.__name__))
        future_imports = [
            node.names[0].name
            for node in ast.iter_child_nodes(src)
            if isinstance(node, ast.ImportFrom) and node.module == "__future__"
            for alias in node.names
        ]
        assert "annotations" in future_imports, (
            "path_cycle_detector.py must have `from __future__ import annotations`"
        )


BASE = pd.Timestamp("2026-06-12T00:00:00Z")


def _asset(code: str) -> Asset:
    return Asset(code=code) if code == "XLM" else Asset(code=code, issuer=f"G{code}ISSUER")


def _payment(src, dst, src_asset, dst_asset, amount, offset_seconds, hops=1):
    return PathPayment(
        id=str(uuid.uuid4()),
        transaction_hash="tx" + uuid.uuid4().hex[:8],
        timestamp=BASE + pd.Timedelta(seconds=offset_seconds),
        source_account=src,
        destination_account=dst,
        source_asset=_asset(src_asset),
        destination_asset=_asset(dst_asset),
        source_amount=amount,
        destination_amount=amount,
        path=[_asset("MID")] * (hops - 1),
        strict_send=True,
    )


def _three_hop_cycle() -> list[PathPayment]:
    """A -> B -> C -> A closing across three separate path payments."""
    return [
        _payment("A", "B", "XLM", "USDC", 1000.0, 0),
        _payment("B", "C", "USDC", "BTC", 1000.0, 60),
        _payment("C", "A", "BTC", "XLM", 1000.0, 120),
    ]


def test_three_account_three_hop_cycle_detected_full_recall():
    cycles = detect_cycles_from_payments(_three_hop_cycle())

    assert len(cycles) == 1
    cycle = cycles[0]
    assert set(cycle["accounts"]) == {"A", "B", "C"}
    assert cycle["cycle_length"] == 3
    assert cycle["cycle_value_xlm"] == 1000.0
    assert cycle["completed_in_seconds"] == 120.0
    # cycle_path repeats the originating asset to close the ring.
    assert cycle["cycle_path"][0] == cycle["cycle_path"][-1]
    assert len(cycle["cycle_path"]) == 4


def test_root_account_filter_restricts_cycles():
    cycles = detect_cycles_from_payments(_three_hop_cycle(), root_accounts={"A"})
    assert len(cycles) == 1

    # A cycle touching none of the roots is excluded.
    assert detect_cycles_from_payments(_three_hop_cycle(), root_accounts={"Z"}) == []


def test_cycle_outside_time_window_is_not_detected():
    payments = [
        _payment("A", "B", "XLM", "USDC", 1000.0, 0),
        _payment("B", "C", "USDC", "BTC", 1000.0, 60),
        _payment("C", "A", "BTC", "XLM", 1000.0, 60 * 60 * 48),  # 48h later
    ]
    cycles = detect_cycles_from_payments(payments, max_time_window=pd.Timedelta(hours=24))
    assert cycles == []


def test_min_cycle_value_threshold():
    payments = [
        _payment("A", "B", "XLM", "USDC", 5.0, 0),  # bottleneck hop
        _payment("B", "C", "USDC", "BTC", 1000.0, 60),
        _payment("C", "A", "BTC", "XLM", 1000.0, 120),
    ]
    assert detect_cycles_from_payments(payments, min_cycle_xlm=10.0) == []
    assert len(detect_cycles_from_payments(payments, min_cycle_xlm=1.0)) == 1


def test_non_cyclic_payments_score_zero():
    payments = [
        _payment("A", "B", "XLM", "USDC", 1000.0, 0),
        _payment("B", "C", "USDC", "BTC", 1000.0, 60),
    ]
    assert detect_cycles_from_payments(payments) == []


def test_cycle_features_for_account():
    cycles = detect_cycles_from_payments(_three_hop_cycle())
    feats = path_cycle_features(cycles, "A")

    assert set(feats) == set(PATH_PAYMENT_CYCLE_FEATURE_NAMES)
    assert feats["path_cycle_count_24h"] == 1.0
    assert feats["path_cycle_xlm_volume_24h"] == 1000.0
    assert feats["max_cycle_length"] == 3.0
    assert feats["cycle_asset_diversity"] >= 3.0

    # An account not in any cycle gets all-zero features.
    assert path_cycle_features(cycles, "Z") == {n: 0.0 for n in PATH_PAYMENT_CYCLE_FEATURE_NAMES}


def test_feature_engineering_wrapper_runs_detection_on_demand():
    feats = path_payment_cycle_features(_three_hop_cycle(), None, "A")
    assert feats["path_cycle_count_24h"] == 1.0

    # No payments -> zero vector without running detection.
    assert path_payment_cycle_features(None, None, "A") == {
        n: 0.0 for n in PATH_PAYMENT_CYCLE_FEATURE_NAMES
    }


def test_cycles_flow_to_alerts_and_get_alerts(tmp_path):
    db_path = str(tmp_path / "alerts.db")
    cycles = detect_cycles_from_payments(_three_hop_cycle())
    alerts = path_payment_cycles_to_alerts(cycles)

    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == AlertType.PATH_PAYMENT_CYCLE.value
    assert alerts[0]["detail"]["cycle_value_xlm"] == 1000.0

    save_alerts(alerts, db_path=db_path)
    stored = get_alerts(alert_type=AlertType.PATH_PAYMENT_CYCLE.value, db_path=db_path)
    assert len(stored) == 1
    assert set(stored[0]["detail"]["accounts"]) == {"A", "B", "C"}


def test_empty_payments_build_empty_graph():
    graph = build_path_payment_graph([])
    assert graph.number_of_nodes() == 0
    assert detect_path_payment_cycles(graph) == []


def test_detection_on_10k_operations_under_10s():
    # 2000 disjoint 3-hop rings (6000 ops) plus 4000 acyclic linear hops.
    payments: list[PathPayment] = []
    for i in range(2000):
        a, b, c = f"R{i}A", f"R{i}B", f"R{i}C"
        payments.append(_payment(a, b, "XLM", "USDC", 1000.0, 0))
        payments.append(_payment(b, c, "USDC", "BTC", 1000.0, 60))
        payments.append(_payment(c, a, "BTC", "XLM", 1000.0, 120))
    for i in range(4000):
        payments.append(_payment(f"L{i}", f"L{i}_dst", "XLM", "USDC", 10.0, 0))

    assert len(payments) == 10000

    start = time.perf_counter()
    cycles = detect_cycles_from_payments(payments)
    elapsed = time.perf_counter() - start

    assert len(cycles) == 2000
    assert elapsed < 10.0


def test_max_cycle_length_must_be_at_least_two():
    graph = build_path_payment_graph(_three_hop_cycle())
    with pytest.raises(ValueError):
        detect_path_payment_cycles(graph, max_cycle_length=1)
