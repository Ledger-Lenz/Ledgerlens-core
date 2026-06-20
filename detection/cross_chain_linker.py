"""Cross-chain wallet relationship resolution and EVM trade pattern analysis.

Links Stellar wallets to their EVM counterparts via bridge transfer records,
and computes aggregate statistics that describe the cross-chain trading behaviour
of the linked EVM wallets.
"""

import logging
from datetime import datetime, timedelta, timezone

from detection.benford_engine import compute_benford_metrics
from detection.storage import get_bridge_transfers
from ingestion.data_models import BridgeTransfer

logger = logging.getLogger("ledgerlens.cross_chain_linker")


class CrossChainLinker:
    """Resolve Stellar <-> EVM wallet links and compute EVM trade patterns."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path

    def link_wallets(self, stellar_wallet: str, lookback_days: int = 90) -> list[str]:
        """Return all EVM wallets linked to `stellar_wallet` via bridge transfers.

        Only transfers within the last `lookback_days` days are considered.
        """
        transfers = get_bridge_transfers(
            stellar_wallet=stellar_wallet,
            since_days=lookback_days,
            db_path=self._db_path,
        )
        seen: set[str] = set()
        result: list[str] = []
        for t in transfers:
            if t.evm_wallet not in seen:
                seen.add(t.evm_wallet)
                result.append(t.evm_wallet)
        return result

    def get_evm_trade_pattern(
        self,
        evm_wallets: list[str],
        chain: str,
        evm_trades: list[dict] | None = None,
        db_path: str | None = None,
    ) -> dict:
        """Compute aggregate EVM trading statistics for a set of linked wallets.

        Parameters
        ----------
        evm_wallets:
            EVM addresses (checksummed) to aggregate over.
        chain:
            Chain name (e.g. "ethereum").
        evm_trades:
            Optional list of EVM trade dicts with keys: wallet_address, amount_in,
            amount_out, counterparty (optional), timestamp (ISO string or datetime).
            When omitted, all statistics default to 0.
        db_path:
            SQLite database path override (for bridge transfer look-ups inside
            round-trip frequency calculation).

        Returns a dict with:
        - total_evm_volume: sum of amount_in across all trades
        - unique_counterparties: count of distinct counterparties
        - round_trip_frequency: fraction of bridge-outs with matching bridge-in within 24h
        - benford_mad: Benford MAD on EVM trade amounts
        """
        db_path = db_path or self._db_path

        if not evm_wallets:
            return {
                "total_evm_volume": 0.0,
                "unique_counterparties": 0,
                "round_trip_frequency": 0.0,
                "benford_mad": 0.0,
            }

        wallet_set = set(evm_wallets)
        trades = [t for t in (evm_trades or []) if t.get("wallet_address") in wallet_set]

        total_volume = sum(float(t.get("amount_in", 0.0)) for t in trades)
        counterparties = {t.get("counterparty") for t in trades if t.get("counterparty")}
        unique_counterparties = len(counterparties)

        amounts = [float(t.get("amount_in", 0.0)) for t in trades if t.get("amount_in", 0.0) > 0]
        benford_mad = compute_benford_metrics(amounts)["mad"] if amounts else 0.0

        round_trip_freq = self._round_trip_frequency(evm_wallets, db_path)

        return {
            "total_evm_volume": total_volume,
            "unique_counterparties": unique_counterparties,
            "round_trip_frequency": round_trip_freq,
            "benford_mad": benford_mad,
        }

    def _round_trip_frequency(
        self, evm_wallets: list[str], db_path: str | None = None
    ) -> float:
        """Fraction of evm_to_stellar transfers that have a matching stellar_to_evm
        transfer from the same EVM wallet within 24 hours.
        """
        if not evm_wallets:
            return 0.0

        all_transfers: list[BridgeTransfer] = []
        for evm_wallet in evm_wallets:
            transfers = get_bridge_transfers(
                evm_wallet=evm_wallet,
                since_days=90,
                db_path=db_path,
            )
            all_transfers.extend(transfers)

        if not all_transfers:
            return 0.0

        outbound = [t for t in all_transfers if t.direction == "evm_to_stellar"]
        inbound = [t for t in all_transfers if t.direction == "stellar_to_evm"]

        if not outbound:
            return 0.0

        window = timedelta(hours=24)
        matched = 0
        for out_tx in outbound:
            for in_tx in inbound:
                if in_tx.evm_wallet == out_tx.evm_wallet:
                    delta = abs(in_tx.timestamp - out_tx.timestamp)
                    if delta <= window:
                        matched += 1
                        break

        return matched / len(outbound)

    def get_cross_chain_links(self, stellar_wallet: str) -> list[dict]:
        """Return cross-chain link metadata for `stellar_wallet`, suitable for API responses."""
        transfers = get_bridge_transfers(
            stellar_wallet=stellar_wallet,
            since_days=90,
            db_path=self._db_path,
        )
        seen: dict[str, dict] = {}
        for t in transfers:
            key = (t.chain, t.evm_wallet)
            if key not in seen or t.timestamp > datetime.fromisoformat(seen[key]["last_bridge_at"]):
                seen[key] = {
                    "chain": t.chain,
                    "evm_wallet": t.evm_wallet,
                    "last_bridge_at": t.timestamp.isoformat(),
                }
        return list(seen.values())
