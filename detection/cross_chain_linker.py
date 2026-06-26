"""Cross-chain wallet relationship resolution and EVM trade pattern analysis.

Links Stellar wallets to their EVM counterparts via bridge transfer records,
and computes aggregate statistics that describe the cross-chain trading behaviour
of the linked EVM wallets. Includes Bayesian probabilistic scoring for
cross-chain identity assertions.
"""

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

from detection.benford_engine import compute_benford_metrics
from detection.storage import get_bridge_transfers
from ingestion.data_models import BridgeTransfer

logger = logging.getLogger("ledgerlens.cross_chain_linker")

CONFIDENCE_THRESHOLD = 0.70
CONFIRMED_THRESHOLD = 0.90
TIMING_SIGMA_SECONDS = 300.0
AMOUNT_TOLERANCE = 0.005  # 0.5%

_EIP55_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class LinkStatus(str, Enum):
    REJECTED = "rejected"
    PROBABLE = "probable"
    CONFIRMED = "confirmed"


@dataclass
class WalletLinkHypothesis:
    stellar_wallet: str
    evm_wallet: str
    evidence_features: Dict[str, float]
    log_likelihood_ratio: float
    confidence: float
    link_status: LinkStatus
    bridge_event_count: int
    created_at: datetime = field(default_factory=datetime.utcnow)


def _validate_evm_address(address: str) -> None:
    """Validate EVM address passes EIP-55 checksum."""
    if not _EIP55_PATTERN.match(address):
        raise ValueError(f"Malformed EVM address: {address!r}")
    try:
        from web3 import Web3
        if not Web3.is_checksum_address(address):
            raise ValueError(f"EVM address fails EIP-55 checksum: {address!r}")
    except ImportError:
        pass


class CrossChainLinker:
    """Resolve Stellar <-> EVM wallet links and compute EVM trade patterns.

    Supports both deterministic linking (link_wallets) and Bayesian
    probabilistic scoring (score_hypothesis) for cross-chain identity
    assertions.
    """

    def __init__(self, db_path: str | None = None, min_confidence: float = CONFIDENCE_THRESHOLD) -> None:
        self._db_path = db_path
        self.min_confidence = min_confidence

    # ── Bayesian scoring ─────────────────────────────────────────────────

    def score_hypothesis(
        self,
        stellar_wallet: str,
        evm_wallet: str,
        bridge_events: list[BridgeTransfer],
    ) -> WalletLinkHypothesis:
        """Compute Bayesian confidence score for the link hypothesis.

        Returns a WalletLinkHypothesis with confidence in [0, 1].
        If no bridge events, returns confidence=0.0 with status=REJECTED.
        """
        _validate_evm_address(evm_wallet)

        if not bridge_events:
            return WalletLinkHypothesis(
                stellar_wallet=stellar_wallet,
                evm_wallet=evm_wallet,
                evidence_features={},
                log_likelihood_ratio=-math.inf,
                confidence=0.0,
                link_status=LinkStatus.REJECTED,
                bridge_event_count=0,
            )

        features = self._compute_features(stellar_wallet, evm_wallet, bridge_events)
        llr = self._log_likelihood_ratio(features)
        confidence = self._sigmoid(llr)
        status = self._classify(confidence)

        return WalletLinkHypothesis(
            stellar_wallet=stellar_wallet,
            evm_wallet=evm_wallet,
            evidence_features=features,
            log_likelihood_ratio=llr,
            confidence=confidence,
            link_status=status,
            bridge_event_count=len(bridge_events),
        )

    def _compute_features(
        self,
        stellar_wallet: str,
        evm_wallet: str,
        bridge_events: list[BridgeTransfer],
    ) -> Dict[str, float]:
        """Compute all evidence features for a link hypothesis."""
        time_deltas = []
        amount_pairs = []

        for i in range(len(bridge_events)):
            for j in range(i + 1, len(bridge_events)):
                a, b = bridge_events[i], bridge_events[j]
                if a.direction != b.direction:
                    delta = abs((a.timestamp - b.timestamp).total_seconds())
                    time_deltas.append(delta)
                    amt_a = a.amount_usd or 0.0
                    amt_b = b.amount_usd or 0.0
                    if amt_a > 0 and amt_b > 0:
                        amount_pairs.append((amt_a, amt_b))

        timing_lr = 1.0
        if time_deltas:
            timing_lr = max(self._timing_likelihood(min(time_deltas)), 1e-9)

        amount_lr = 1.0
        if amount_pairs:
            amount_lrs = [self._amount_likelihood(a, b) for a, b in amount_pairs]
            amount_lr = max(amount_lrs)

        direction_lr = self._direction_consistency_likelihood(bridge_events)

        address_lr = self._address_pattern_likelihood(stellar_wallet, evm_wallet)

        return {
            "timing_similarity": timing_lr,
            "amount_match": amount_lr,
            "direction_consistency": direction_lr,
            "address_pattern": address_lr,
        }

    def _timing_likelihood(self, time_delta_seconds: float) -> float:
        """Gaussian likelihood ratio: P(delta | same_entity) / P(delta | random).

        Uses a Gaussian with sigma=300s for same-entity, uniform over [0, 3600]
        for random pairs.
        """
        gaussian = math.exp(-0.5 * (time_delta_seconds / TIMING_SIGMA_SECONDS) ** 2)
        uniform = 1.0 / 3600.0
        return gaussian / (uniform + 1e-9)

    def _amount_likelihood(self, stellar_amount: float, evm_amount: float) -> float:
        """Returns 10.0 (strong evidence) if within tolerance, else 1.0 (neutral)."""
        if stellar_amount == 0:
            return 1.0
        pct_diff = abs(stellar_amount - evm_amount) / stellar_amount
        return 10.0 if pct_diff <= AMOUNT_TOLERANCE else 1.0

    def _direction_consistency_likelihood(self, bridge_events: list[BridgeTransfer]) -> float:
        """Score based on bridge direction patterns consistent with wash trading.

        Round-trip patterns (stellar_to_evm followed by evm_to_stellar or vice versa)
        are strong evidence of same-entity linking.
        """
        if len(bridge_events) < 2:
            return 1.0

        directions = [e.direction for e in sorted(bridge_events, key=lambda e: e.timestamp)]
        transitions = 0
        for i in range(1, len(directions)):
            if directions[i] != directions[i - 1]:
                transitions += 1

        max_transitions = len(directions) - 1
        if max_transitions == 0:
            return 1.0

        ratio = transitions / max_transitions
        return 1.0 + 4.0 * ratio

    def _address_pattern_likelihood(self, stellar_wallet: str, evm_wallet: str) -> float:
        """Weak prior based on address-pattern heuristics.

        Checks if the last 4 hex chars of the EVM address match
        the last 4 chars of the Stellar address (base32). This is a
        very weak signal but can marginally improve confidence.
        """
        if len(evm_wallet) < 6 or len(stellar_wallet) < 6:
            return 1.0
        evm_suffix = evm_wallet[-4:].lower()
        stellar_suffix = stellar_wallet[-4:].lower()
        if evm_suffix == stellar_suffix:
            return 1.5
        return 1.0

    def _log_likelihood_ratio(self, features: Dict[str, float]) -> float:
        """Sum of log-likelihood ratios across all evidence features."""
        return sum(math.log(max(v, 1e-9)) for v in features.values())

    def _sigmoid(self, x: float) -> float:
        """Logistic sigmoid clamped to [0.0, 1.0]."""
        if x >= 700:
            return 1.0
        if x <= -700:
            return 0.0
        return max(0.0, min(1.0, 1.0 / (1.0 + math.exp(-x))))

    def _classify(self, confidence: float) -> LinkStatus:
        """Classify confidence into link status."""
        if confidence >= CONFIRMED_THRESHOLD:
            return LinkStatus.CONFIRMED
        elif confidence >= self.min_confidence:
            return LinkStatus.PROBABLE
        return LinkStatus.REJECTED

    # ── Persistence ──────────────────────────────────────────────────────

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cross_chain_links (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                stellar_wallet  TEXT NOT NULL,
                evm_wallet      TEXT NOT NULL,
                confidence      REAL NOT NULL,
                link_status     TEXT NOT NULL,
                log_likelihood_ratio REAL NOT NULL,
                evidence_json   TEXT NOT NULL,
                bridge_event_count INTEGER NOT NULL,
                created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(stellar_wallet, evm_wallet)
            );
            CREATE INDEX IF NOT EXISTS idx_links_stellar ON cross_chain_links(stellar_wallet);
            CREATE INDEX IF NOT EXISTS idx_links_confidence ON cross_chain_links(confidence DESC);
        """)

    def persist_hypothesis(self, hypothesis: WalletLinkHypothesis) -> None:
        """Persist to cross_chain_links SQLite table if status != REJECTED."""
        if hypothesis.link_status == LinkStatus.REJECTED:
            return

        db_path = self._db_path or "./ledgerlens.db"
        conn = sqlite3.connect(db_path)
        try:
            self._ensure_table(conn)
            conn.execute(
                """INSERT INTO cross_chain_links
                   (stellar_wallet, evm_wallet, confidence, link_status,
                    log_likelihood_ratio, evidence_json, bridge_event_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stellar_wallet, evm_wallet) DO UPDATE SET
                       confidence = excluded.confidence,
                       link_status = excluded.link_status,
                       log_likelihood_ratio = excluded.log_likelihood_ratio,
                       evidence_json = excluded.evidence_json,
                       bridge_event_count = excluded.bridge_event_count,
                       created_at = excluded.created_at
                """,
                (
                    hypothesis.stellar_wallet,
                    hypothesis.evm_wallet,
                    hypothesis.confidence,
                    hypothesis.link_status.value,
                    hypothesis.log_likelihood_ratio,
                    json.dumps(hypothesis.evidence_features),
                    hypothesis.bridge_event_count,
                    hypothesis.created_at.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_accepted_links(
        self, stellar_wallet: str, min_confidence: float | None = None
    ) -> list[WalletLinkHypothesis]:
        """Retrieve all accepted hypotheses for a Stellar wallet, sorted by confidence desc."""
        min_conf = min_confidence if min_confidence is not None else self.min_confidence
        db_path = self._db_path or "./ledgerlens.db"
        try:
            conn = sqlite3.connect(db_path)
            self._ensure_table(conn)
            rows = conn.execute(
                """SELECT stellar_wallet, evm_wallet, confidence, link_status,
                          log_likelihood_ratio, evidence_json, bridge_event_count, created_at
                   FROM cross_chain_links
                   WHERE stellar_wallet = ? AND confidence >= ?
                   ORDER BY confidence DESC""",
                (stellar_wallet, min_conf),
            ).fetchall()
            conn.close()
        except Exception:
            return []

        results = []
        for row in rows:
            results.append(WalletLinkHypothesis(
                stellar_wallet=row[0],
                evm_wallet=row[1],
                evidence_features=json.loads(row[5]),
                log_likelihood_ratio=row[4],
                confidence=row[2],
                link_status=LinkStatus(row[3]),
                bridge_event_count=row[6],
                created_at=datetime.fromisoformat(row[7]),
            ))
        return results

    # ── Deterministic linking (existing) ─────────────────────────────────

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
