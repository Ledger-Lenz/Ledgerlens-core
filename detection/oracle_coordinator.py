from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from detection.oracle_node import OracleNode

if TYPE_CHECKING:
    from detection.soroban_publisher import SorobanPublisher

logger = logging.getLogger("ledgerlens.oracle_coordinator")


@dataclass
class QuorumSignature:
    message_bytes: bytes            # canonical message that was signed
    signatures: list[tuple[str, str]]  # [(public_key_hex, signature_hex), ...]
    signers_count: int
    threshold: int
    is_valid_quorum: bool           # True if signers_count >= threshold


@dataclass
class AggregationResult:
    """Outcome of a BFT-tolerant quorum aggregation round.

    Fault-tolerance model (3f+1):
      * ``n`` total nodes, ``f = (n - 1) // 3`` tolerated Byzantine (malicious
        or faulty) nodes.
      * A quorum requires ``2f + 1`` agreeing reports, which guarantees that
        any two quorums intersect in at least one honest node.
      * Aggregation is correct as long as at most ``f`` nodes misreport.
    """

    value: int | None                       # agreed score, None if no quorum
    agreeing: list[str]                     # node names in the winning quorum
    dissenting: list[str]                   # node names whose report differed
    slashed: list[str]                      # dissenting nodes proven inconsistent
    quorum_size: int                        # number of agreeing reports
    required_quorum: int                    # 2f + 1
    tolerated_faults: int                   # f
    is_valid_quorum: bool


class OracleCoordinator:
    """
    Coordinates threshold signatures across multiple OracleNodes and performs
    BFT-tolerant quorum aggregation with slashing for provably inconsistent
    reports.
    """

    # A node is considered dead if no heartbeat arrives within this window.
    HEARTBEAT_TIMEOUT_SECONDS: float = 30.0
    # Minimum number of active nodes required to keep quorum achievable.
    MIN_ACTIVE_NODES: int = 2
    # Alert when active nodes drop to (or below) this many.
    ALERT_ACTIVE_NODES: int = 3

    def __init__(self, nodes: list[OracleNode], threshold: int = 3):
        if threshold > len(nodes):
            raise ValueError(f"Threshold {threshold} > node count {len(nodes)}")
        self.nodes = nodes
        self.threshold = threshold
        # Byzantine fault tolerance: tolerate f faulty nodes out of 3f+1.
        self.tolerated_faults = (len(nodes) - 1) // 3
        # A quorum of 2f+1 guarantees intersection with any other quorum.
        self.required_quorum = 2 * self.tolerated_faults + 1
        # Economic disincentive: stake slashed per proven misreport.
        self.slash_amount = 1
        self.slashed_nodes: dict[str, int] = {}

    def collect_signatures(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
    ) -> QuorumSignature:
        """Collect signatures from active nodes; stop after threshold is reached."""
        self.reconfigure_quorum()
        message = OracleNode._canonical_message(
            wallet,
            asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        )
        signatures = []
        for node in self.active_nodes():
            try:
                sig = node.sign_score_submission(
                    wallet,
                    asset_pair,
                    score,
                    benford_flag,
                    ml_flag,
                    timestamp,
                    confidence,
                    model_version,
                )
                signatures.append((node.public_key_hex, sig.hex()))
                if len(signatures) >= self.threshold:
                    break      # Short-circuit: quorum reached
            except Exception as e:
                logger.warning("Oracle %s failed to sign: %s", node.name, e)
        return QuorumSignature(
            message_bytes=message,
            signatures=signatures,
            signers_count=len(signatures),
            threshold=self.threshold,
            is_valid_quorum=len(signatures) >= self.threshold,
        )

    def aggregate_reports(
        self,
        reports: dict[str, int],
        slash: bool = True,
    ) -> AggregationResult:
        """Aggregate per-node score reports using a BFT-tolerant quorum rule.

        The winning value is the one reported by at least ``2f + 1`` nodes.
        Nodes whose report differs from the winning quorum are provably
        inconsistent with consensus and are slashed (their stake is reduced).
        """
        counts = Counter(reports.values())
        required = self.required_quorum
        winner: int | None = None
        for value, count in counts.most_common():
            if count >= required:
                winner = value
                break

        if winner is None:
            logger.error(
                "No BFT quorum: required %d agreeing reports, got %s",
                required,
                dict(counts),
            )
            return AggregationResult(
                value=None,
                agreeing=[],
                dissenting=list(reports.keys()),
                slashed=[],
                quorum_size=0,
                required_quorum=required,
                tolerated_faults=self.tolerated_faults,
                is_valid_quorum=False,
            )

        agreeing = [name for name, value in reports.items() if value == winner]
        dissenting = [name for name, value in reports.items() if value != winner]
        slashed: list[str] = []
        if slash:
            for name in dissenting:
                self.slashed_nodes[name] = self.slashed_nodes.get(name, 0) + self.slash_amount
                slashed.append(name)
                logger.warning(
                    "Slashing oracle %s: report %s inconsistent with quorum %s",
                    name,
                    reports[name],
                    winner,
                )

        return AggregationResult(
            value=winner,
            agreeing=agreeing,
            dissenting=dissenting,
            slashed=slashed,
            quorum_size=len(agreeing),
            required_quorum=required,
            tolerated_faults=self.tolerated_faults,
            is_valid_quorum=True,
        )

    def submit_with_quorum(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
        publisher: "SorobanPublisher",
    ) -> bool:
        """Collects quorum signatures and forwards to the publisher."""
        quorum = self.collect_signatures(
            wallet,
            asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        )
        if not quorum.is_valid_quorum:
            logger.error(
                "Quorum not reached: %d/%d signatures",
                quorum.signers_count,
                self.threshold,
            )
            return False
        # Call oracle_aggregator Soroban contract
        return publisher.submit_with_quorum(
            wallet,
            asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
            quorum,
        )
