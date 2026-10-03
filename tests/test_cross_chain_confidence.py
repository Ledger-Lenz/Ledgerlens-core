"""Calibrated confidence scoring for cross-chain wallet linkage (Issue #1035)."""

from datetime import datetime, timedelta, timezone

import numpy as np
from eth_utils import to_checksum_address
from stellar_sdk import Keypair

from detection.cross_chain_linker import CrossChainLinker
from ingestion.data_models import BridgeTransfer

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STELLAR = Keypair.random().public_key


def _pair(rng, delta_s, amount_out, amount_in, i):
    evm = to_checksum_address("0x" + rng.bytes(20).hex())
    common = {"chain": "ethereum", "evm_wallet": evm, "stellar_wallet": STELLAR, "token": "USDC"}
    return evm, [
        BridgeTransfer(
            direction="stellar_to_evm",
            amount_usd=amount_out,
            tx_hash_evm=f"0x{i:064x}",
            timestamp=NOW,
            **common,
        ),
        BridgeTransfer(
            direction="evm_to_stellar",
            amount_usd=amount_in,
            tx_hash_evm=f"0x{i + 1:064x}",
            timestamp=NOW + timedelta(seconds=delta_s),
            **common,
        ),
    ]


def _labeled_set(seed, n=400):
    """Known true links: fast, usually amount-matched round trips.
    Known false links: unrelated wallets with arbitrary timing and amounts."""
    rng = np.random.default_rng(seed)
    samples = []
    for i in range(n):
        label = int(i % 2 == 0)
        amt = float(rng.uniform(100, 10_000))
        if label:
            delta = abs(rng.normal(0, 600))
            amt_in = (
                amt * (1 + rng.uniform(-0.003, 0.003))
                if rng.random() < 0.7
                else amt * rng.uniform(0.5, 1.5)
            )
        else:
            delta = rng.uniform(0, 4 * 3600)
            amt_in = (
                amt * (1 + rng.uniform(-0.003, 0.003))
                if rng.random() < 0.1
                else amt * rng.uniform(0.5, 1.5)
            )
        samples.append((_pair(rng, delta, amt, amt_in, 2 * i), label))
    return samples


def _llrs(linker, samples):
    return [
        linker.score_hypothesis(STELLAR, evm, events).log_likelihood_ratio
        for (evm, events), _ in samples
    ]


def _ece(probs, labels, bins=10):
    probs, labels = np.asarray(probs), np.asarray(labels)
    idx = np.minimum((probs * bins).astype(int), bins - 1)
    return sum(
        abs(probs[idx == b].mean() - labels[idx == b].mean()) * (idx == b).mean()
        for b in range(bins)
        if (idx == b).any()
    )


def test_confidence_is_calibrated_on_held_out_labeled_pairs():
    linker = CrossChainLinker()
    train, test = _labeled_set(seed=1), _labeled_set(seed=2)

    raw_probs = [linker.score_hypothesis(STELLAR, evm, ev).confidence for (evm, ev), _ in test]
    linker.fit_calibration(_llrs(linker, train), [y for _, y in train])
    cal_probs = [linker.score_hypothesis(STELLAR, evm, ev).confidence for (evm, ev), _ in test]
    labels = [y for _, y in test]

    def brier(p):
        return float(np.mean((np.asarray(p) - np.asarray(labels)) ** 2))

    assert _ece(cal_probs, labels) < 0.08
    assert _ece(cal_probs, labels) < _ece(raw_probs, labels)
    assert brier(cal_probs) < brier(raw_probs)
    # Confidence is graded, not a binary match/no-match.
    assert len({round(p, 3) for p in cal_probs}) > 10


def test_true_links_score_higher_than_false_links():
    linker = CrossChainLinker()
    train = _labeled_set(seed=3)
    linker.fit_calibration(_llrs(linker, train), [y for _, y in train])
    test = _labeled_set(seed=4)
    probs = np.array(
        [linker.score_hypothesis(STELLAR, evm, ev).confidence for (evm, ev), _ in test]
    )
    labels = np.array([y for _, y in test])
    assert probs[labels == 1].mean() > probs[labels == 0].mean() + 0.3
