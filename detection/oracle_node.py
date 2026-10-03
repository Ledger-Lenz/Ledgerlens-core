from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ORACLE_DOMAIN_SEPARATOR = b"LedgerLens-Oracle-v2"
SOROBAN_SYMBOL_SCVAL_TYPE = 15
MAX_SYMBOL_LENGTH = 32


@dataclass(frozen=True)
class OracleReport:
    """A single oracle node's signed score report."""

    node: str
    score: int
    confidence: int
    timestamp: int
    signature: bytes = b""


@dataclass
class QuorumResult:
    """Outcome of BFT quorum aggregation over a set of oracle reports."""

    agreed: bool
    score: int | None
    confidence: int | None
    quorum_size: int
    total_nodes: int
    faulty_bound: int
    agreeing_nodes: list[str] = field(default_factory=list)
    slashed_nodes: list[str] = field(default_factory=list)


class BFTQuorumAggregator:
    """
    BFT-tolerant quorum aggregation for oracle score reports.

    Fault-tolerance model (3f+1):
      * The oracle set has ``n`` nodes and tolerates up to ``f = (n - 1) // 3``
        Byzantine (malicious or faulty) nodes.
      * A quorum is any set of ``2f + 1`` nodes. Two quorums always intersect in
        at least ``f + 1`` nodes, so at least one honest node is shared, which
        prevents two conflicting values from both reaching quorum.
      * A value is accepted only when a strict majority of the *whole* node set
        (``> n / 2``) reports it, guaranteeing agreement even with ``f`` liars.

    Economic assumptions:
      * Every node posts a slashable bond. Reports that are provably inconsistent
        with the accepted quorum value (i.e. a node reported a different value
        than the one that reached quorum) are slashed.
      * Slashing is only applied when a quorum was actually reached, so honest
        minority reports during a partition are never penalised.
    """

    def __init__(self, total_nodes: int):
        if total_nodes < 1:
            raise ValueError("total_nodes must be >= 1")
        self.total_nodes = total_nodes
        self.faulty_bound = (total_nodes - 1) // 3
        self.quorum_size = 2 * self.faulty_bound + 1

    def aggregate(self, reports: Iterable[OracleReport]) -> QuorumResult:
        """
        Aggregate reports into a single quorum value.

        Returns a :class:`QuorumResult`. When no value reaches a strict majority
        of the node set, ``agreed`` is ``False`` and no node is slashed.
        """
        reports = list(reports)
        if not reports:
            return QuorumResult(
                agreed=False,
                score=None,
                confidence=None,
                quorum_size=self.quorum_size,
                total_nodes=self.total_nodes,
                faulty_bound=self.faulty_bound,
            )

        # Tally votes keyed by (score, confidence) so a value only wins when a
        # strict majority of the whole node set agrees on it.
        tally: dict[tuple[int, int], list[OracleReport]] = {}
        for report in reports:
            tally.setdefault((report.score, report.confidence), []).append(report)

        majority = self.total_nodes // 2 + 1
        winner: tuple[int, int] | None = None
        winner_reports: list[OracleReport] = []
        for value, group in tally.items():
            if len(group) >= majority and len(group) > len(winner_reports):
                winner = value
                winner_reports = group

        if winner is None:
            return QuorumResult(
                agreed=False,
                score=None,
                confidence=None,
                quorum_size=self.quorum_size,
                total_nodes=self.total_nodes,
                faulty_bound=self.faulty_bound,
            )

        score, confidence = winner
        agreeing = [r.node for r in winner_reports]
        slashed = [r.node for r in reports if (r.score, r.confidence) != winner]

        return QuorumResult(
            agreed=True,
            score=score,
            confidence=confidence,
            quorum_size=self.quorum_size,
            total_nodes=self.total_nodes,
            faulty_bound=self.faulty_bound,
            agreeing_nodes=agreeing,
            slashed_nodes=slashed,
        )

    def slash_inconsistent(
        self,
        reports: Iterable[OracleReport],
        consensus: Mapping[str, int],
    ) -> list[str]:
        """
        Return nodes whose reported score is provably inconsistent with the
        accepted consensus value for their node id.

        ``consensus`` maps node id -> accepted score. Only nodes present in the
        consensus map can be slashed; unknown nodes are ignored.
        """
        slashed: list[str] = []
        for report in reports:
            expected = consensus.get(report.node)
            if expected is not None and report.score != expected:
                slashed.append(report.node)
        return slashed


class OracleNode:
    """
    Oracle node encapsulating an ED25519 keypair for threshold signing.
    """

    def __init__(self, name: str, private_key_env_var: str):
        """
        Load ED25519 private key from environment variable (32 hex-encoded bytes).
        Raises EnvironmentError if the variable is not set.
        """
        raw = os.environ.get(private_key_env_var)
        if not raw:
            raise EnvironmentError(f"Oracle key not set: {private_key_env_var}")
        
        try:
            key_bytes = bytes.fromhex(raw)
            if len(key_bytes) != 32:
                raise ValueError("Key must be 32 bytes")
            self._private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
        except Exception as e:
            raise EnvironmentError(f"Invalid oracle key format in {private_key_env_var}: {e}")
            
        self.name = name
        self.last_seen: float | None = None

    @property
    def public_key_hex(self) -> str:
        pub = self._private_key.public_key()
        return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign_score_submission(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
    ) -> bytes:
        """
        Sign every caller-controlled field forwarded to ledgerlens-score.

        Returns 64-byte ED25519 signature.
        """
        message = self._canonical_message(
            wallet,
            asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        )
        sig = self._private_key.sign(message)
        self.last_seen = time.time()
        return sig

    @staticmethod
    def _canonical_message(
        wallet: str,
        asset_pair: str,
        score: int,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: int,
        confidence: int,
        model_version: int,
    ) -> bytes:
        OracleNode._validate_payload(score, confidence, timestamp, model_version)
        body = (
            ORACLE_DOMAIN_SEPARATOR
            + wallet.encode("utf-8")
            + b"|"
            + OracleNode._symbol_xdr(asset_pair)
            + b"|"
            + struct.pack(">I", score)
            + struct.pack(">?", benford_flag)
            + struct.pack(">?", ml_flag)
            + struct.pack(">Q", timestamp)
            + struct.pack(">I", confidence)
            + struct.pack(">I", model_version)
        )
        return hashlib.sha256(body).digest()

    @staticmethod
    def _symbol_xdr(value: str) -> bytes:
        encoded = value.encode("ascii")
        if (
            len(encoded) > MAX_SYMBOL_LENGTH
            or not encoded
            or any(not (chr(byte).isalnum() or byte == ord("_")) for byte in encoded)
        ):
            raise ValueError(
                "asset_pair must be a non-empty Soroban Symbol "
                "(ASCII alphanumeric/underscore, at most 32 bytes)"
            )
        padding = b"\x00" * ((-len(encoded)) % 4)
        return struct.pack(">iI", SOROBAN_SYMBOL_SCVAL_TYPE, len(encoded)) + encoded + padding

    @staticmethod
    def _validate_payload(score: int, confidence: int, timestamp: int, model_version: int) -> None:
        if not 0 <= score <= 100:
            raise ValueError("score must be between 0 and 100")
        if not 0 <= confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if not 0 <= timestamp <= 2**64 - 1:
            raise ValueError("timestamp must fit u64")
        if not 0 <= model_version <= 2**32 - 1:
            raise ValueError("model_version must fit u32")
