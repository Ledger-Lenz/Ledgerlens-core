"""Reorg-safe finality tracking for EVM-side ingestion.

EVM data is only treated as final once it is ``confirmation_depth`` blocks
below the chain head.  :class:`FinalityTracker` also remembers the block hash
of every ingested block (for ``reorg_check_blocks`` blocks behind the
finalized head) and, on each ingestion cycle, compares them against the
current canonical chain.  Blocks whose hash changed were orphaned by a reorg
— including reorgs deeper than the confirmation depth — and the transactions
ingested from them are returned so callers can retract them from downstream
stores.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("ledgerlens.evm_finality")


@dataclass
class _TrackedBlock:
    block_hash: str
    tx_hashes: set[str] = field(default_factory=set)


class FinalityTracker:
    """Track ingested EVM block hashes and detect reorgs.

    Parameters
    ----------
    confirmation_depth:
        Blocks below the head before data is considered final.
    reorg_check_blocks:
        How many blocks below the finalized head ingested hashes are kept and
        re-checked for deep reorgs.
    """

    def __init__(self, confirmation_depth: int = 12, reorg_check_blocks: int = 128) -> None:
        if confirmation_depth < 0:
            raise ValueError(f"confirmation_depth must be >= 0, got {confirmation_depth}")
        self.confirmation_depth = confirmation_depth
        self.reorg_check_blocks = reorg_check_blocks
        self._blocks: dict[int, _TrackedBlock] = {}

    def finalized_head(self, latest_block: int) -> int:
        """Highest block number treated as final for *latest_block*."""
        return max(0, latest_block - self.confirmation_depth)

    def is_final(self, block_number: int, latest_block: int) -> bool:
        return block_number <= self.finalized_head(latest_block)

    def record(self, block_number: int, block_hash: str, tx_hash: str) -> None:
        """Remember that *tx_hash* was ingested from block *block_number*/*block_hash*."""
        if not block_hash:
            return
        tracked = self._blocks.get(block_number)
        if tracked is None or tracked.block_hash != block_hash:
            tracked = self._blocks[block_number] = _TrackedBlock(block_hash)
        tracked.tx_hashes.add(tx_hash)

    def check_reorgs(
        self,
        latest_block: int,
        get_canonical_hash: Callable[[int], str | None],
    ) -> dict[int, set[str]]:
        """Compare tracked hashes with the canonical chain.

        Returns ``{orphaned_block_number: {tx_hash, ...}}`` and forgets those
        blocks.  Blocks older than the check window are pruned.
        """
        oldest = self.finalized_head(latest_block) - self.reorg_check_blocks
        for number in [n for n in self._blocks if n < oldest]:
            del self._blocks[number]

        orphaned: dict[int, set[str]] = {}
        for number, tracked in sorted(self._blocks.items()):
            canonical = get_canonical_hash(number)
            if canonical is not None and canonical.lower() != tracked.block_hash.lower():
                orphaned[number] = tracked.tx_hashes
        for number in orphaned:
            del self._blocks[number]
        if orphaned:
            logger.warning(
                "EVM reorg detected: %d orphaned block(s) %s; retracting %d tx(s)",
                len(orphaned),
                sorted(orphaned),
                sum(len(v) for v in orphaned.values()),
            )
        return orphaned
