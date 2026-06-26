"""Cross-chain bridge transaction correlation for multi-network wash detection.

Identifies round-trip bridge patterns where a wallet bridges assets from
Stellar to EVM and back within a configurable time window, computing a
correlation score that feeds into the risk model as an additional feature.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ingestion.data_models import BridgeTransfer

logger = logging.getLogger("ledgerlens.cross_chain_correlator")

# Default time window for matching bridge-in / bridge-out events (hours).
DEFAULT_WINDOW_HOURS = 24

# Amount match tolerance (fraction) — after estimated bridge fees.
AMOUNT_MATCH_TOLERANCE = 0.05


def _parse_timestamp(ts) -> datetime:
    """Parse a timestamp from datetime or ISO-format string."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class CrossChainCorrelator:
    """Correlate bridge-in and bridge-out transactions to detect round-trip
    bridge patterns indicative of multi-network wash trading.

    For each wallet, finds pairs of (Stellar → EVM bridge-out,
    EVM → Stellar bridge-in) within a configurable time window and
    computes a correlation score based on amount similarity, timing
    proximity, and EVM intermediate hop count.
    """

    def __init__(
        self,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        amount_tolerance: float = AMOUNT_MATCH_TOLERANCE,
    ) -> None:
        """Initialise the correlator.

        Parameters
        ----------
        window_hours:
            Maximum time window in hours for matching bridge-out and
            bridge-in events.  Default is 24.
        amount_tolerance:
            Fractional tolerance for matching amounts (default 0.05,
            i.e. 5%).
        """
        self._window = timedelta(hours=window_hours)
        self._amount_tolerance = amount_tolerance

    def compute_round_trip_score(
        self,
        wallet: str,
        transfers: list[BridgeTransfer],
    ) -> float:
        """Compute a cross-chain round-trip correlation score in [0, 1].

        A score near 1.0 indicates strong evidence of round-trip bridge
        patterns (assets moving Stellar → EVM → Stellar within the
        window with matching amounts).

        Parameters
        ----------
        wallet:
            Stellar wallet address.
        transfers:
            List of bridge transfers involving this wallet.

        Returns
        -------
        float
            Score in [0, 1].  Returns 0.0 when fewer than 2 transfers
            exist or no matching pairs are found.
        """
        if len(transfers) < 2:
            return 0.0

        # Separate outbound (Stellar→EVM) and inbound (EVM→Stellar)
        outbound = [
            t for t in transfers if t.direction == "stellar_to_evm"
        ]
        inbound = [
            t for t in transfers if t.direction == "evm_to_stellar"
        ]

        if not outbound or not inbound:
            return 0.0

        # Find matching pairs: for each outbound, look for an inbound
        # from the same EVM wallet within the time window with a similar
        # amount.
        matched_pairs: list[tuple[float, float, int]] = []
        for out in outbound:
            out_ts = _parse_timestamp(out.timestamp)
            for inp in inbound:
                in_ts = _parse_timestamp(inp.timestamp)
                if inp.evm_wallet != out.evm_wallet:
                    continue
                # Inbound must happen after outbound within window
                delta = in_ts - out_ts
                if delta < timedelta(0) or delta > self._window:
                    continue
                # Amount similarity check
                out_amt = out.amount_usd or 0.0
                in_amt = inp.amount_usd or 0.0
                if out_amt <= 0 or in_amt <= 0:
                    continue
                ratio = min(out_amt, in_amt) / max(out_amt, in_amt)
                if ratio < (1.0 - self._amount_tolerance):
                    continue

                # Timing score: closer in time = higher score (0..1)
                timing_score = 1.0 - (delta.total_seconds() / self._window.total_seconds())

                # Amount score: closer amounts = higher score (0..1)
                amount_score = ratio

                # Hop score: fewer hops is more suspicious (direct round-trip)
                # but we don't have hop count in BridgeTransfer yet,
                # so we default to a neutral value
                hop_count = getattr(inp, "evm_hop_count", 1)
                hop_score = 1.0 / max(hop_count, 1)

                pair_score = (amount_score * 0.5 + timing_score * 0.3 + hop_score * 0.2)
                matched_pairs.append((pair_score, delta.total_seconds(), hop_count))

        if not matched_pairs:
            return 0.0

        # Aggregate: best single pair + diminishing returns from additional pairs
        best = max(pair[0] for pair in matched_pairs)
        bonus = min(0.3, 0.05 * (len(matched_pairs) - 1))
        return min(1.0, best + bonus)