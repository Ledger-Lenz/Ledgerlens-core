from __future__ import annotations

import logging
from dataclasses import dataclass
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


class OracleCoordinator:
    """
    Coordinates threshold signatures across multiple OracleNodes.
    """

    def __init__(self, nodes: list[OracleNode], threshold: int = 3):
        if threshold > len(nodes):
            raise ValueError(f"Threshold {threshold} > node count {len(nodes)}")
        self.nodes = nodes
        self.threshold = threshold

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
        """Collect signatures from all nodes; stop after threshold is reached."""
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
        for node in self.nodes:
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
