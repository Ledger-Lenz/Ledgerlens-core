"""Bridge-spanning path-payment cycles (issue #1036).

Synthetic route: A -> B -> C on Stellar via path payments, C bridges out to
an EVM wallet, and the same EVM wallet bridges back to A. The round trip is
only visible once the bridge hop is stitched into the hop graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from detection.path_payment_engine import PathCycleDetector, bridge_hop_edges
from ingestion.data_models import BridgeTransfer

A = "G" + "A" * 55
B = "G" + "B" * 55
C = "G" + "C" * 55
EVM = "0x" + "1" * 40
OTHER_EVM = "0x" + "2" * 40
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _hop(op_id: str, src: str, dst: str, amount: float, minutes: int) -> dict:
    return {
        "id": op_id,
        "source_account": src,
        "to": dst,
        "asset_code": "USDC",
        "destination_asset_code": "USDC",
        "amount": str(amount),
        "created_at": (T0 + timedelta(minutes=minutes)).isoformat(),
    }


def _bridge(direction: str, stellar: str, evm: str, amount: float, minutes: int, tx: str):
    return BridgeTransfer(
        chain="ethereum",
        direction=direction,
        evm_wallet=evm,
        stellar_wallet=stellar,
        amount_usd=amount,
        token="USDC",
        tx_hash_evm=tx,
        timestamp=T0 + timedelta(minutes=minutes),
    )


def _stellar_legs(detector: PathCycleDetector) -> list:
    return detector.ingest([_hop("1", A, B, 1000.0, 0), _hop("2", B, C, 1000.0, 1)])


def test_bridge_spanning_circular_route_is_flagged():
    detector = PathCycleDetector()
    assert _stellar_legs(detector) == []  # no cycle on Stellar alone

    cycles = detector.ingest_bridge_transfers(
        [
            _bridge("stellar_to_evm", C, EVM, 1000.0, 2, "0xout"),
            _bridge("evm_to_stellar", A, EVM, 990.0, 8, "0xin"),
        ]
    )

    from_a = [c for c in cycles if c.origin_wallet == A]
    assert len(from_a) == 1
    cycle = from_a[0]
    assert cycle.crosses_bridge
    # End-to-end: A -> B -> C -(bridge via EVM)-> A
    assert [(h.src_wallet, h.dst_wallet) for h in cycle.hops] == [(A, B), (B, C), (C, A)]
    assert cycle.recovery_ratio == 0.99
    assert detector.get_features(A)["path_cycle_count"] == 1.0


def test_bridge_hop_resolves_identity_through_shared_evm_wallet():
    edges = bridge_hop_edges(
        [
            _bridge("stellar_to_evm", C, EVM, 1000.0, 2, "0xout"),
            _bridge("evm_to_stellar", A, EVM, 990.0, 8, "0xin"),
        ]
    )
    assert len(edges) == 1
    edge = edges[0]
    assert (edge.src_wallet, edge.dst_wallet) == (C, A)
    assert edge.operation_id == "bridge:0xout:0xin"


def test_unrelated_evm_wallet_does_not_close_the_route():
    detector = PathCycleDetector()
    _stellar_legs(detector)
    cycles = detector.ingest_bridge_transfers(
        [
            _bridge("stellar_to_evm", C, EVM, 1000.0, 2, "0xout"),
            _bridge("evm_to_stellar", A, OTHER_EVM, 990.0, 8, "0xin"),
        ]
    )
    assert cycles == []
