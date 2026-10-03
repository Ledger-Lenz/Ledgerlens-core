"""Secure aggregation via pairwise masking (Issue #1034)."""

import numpy as np
import pytest

from detection.federated.secure_agg import (
    SecureAggParticipant,
    SecureAggregator,
    decode,
)

IDS = ["alice", "bob", "carol", "dave"]


def _round(round_id="r1"):
    rng = np.random.default_rng(0)
    updates = {pid: rng.normal(size=16) for pid in IDS}
    participants = {pid: SecureAggParticipant(pid) for pid in IDS}
    keys = {pid: p.public_key_bytes() for pid, p in participants.items()}
    masked = {pid: participants[pid].mask_update(updates[pid], keys, round_id) for pid in IDS}
    return updates, masked


def test_masks_cancel_when_all_participants_report():
    updates, masked = _round()
    agg = SecureAggregator(IDS, "r1")
    for pid in IDS:
        agg.submit(pid, masked[pid])
    np.testing.assert_allclose(agg.aggregate(), sum(updates.values()), atol=1e-5)


def test_aggregate_refused_when_a_participant_is_missing():
    _, masked = _round()
    agg = SecureAggregator(IDS, "r1")
    for pid in IDS[:-1]:
        agg.submit(pid, masked[pid])
    with pytest.raises(RuntimeError, match="missing participants"):
        agg.aggregate()


def test_partial_sum_does_not_reveal_updates():
    updates, masked = _round()
    partial = decode(sum(masked[pid] for pid in IDS[:-1]))
    expected = sum(updates[pid] for pid in IDS[:-1])
    assert not np.allclose(partial, expected, atol=1.0)


def test_single_masked_update_does_not_reveal_raw_update():
    updates, masked = _round()
    assert not np.allclose(decode(masked["alice"]), updates["alice"], atol=1.0)


def test_coordinator_exposes_no_per_participant_accessor():
    public = {n for n in dir(SecureAggregator) if not n.startswith("_")}
    assert public == {"submit", "aggregate"}


def test_rejects_too_few_participants_and_duplicates():
    with pytest.raises(ValueError):
        SecureAggregator(["a", "b"], "r1")
    _, masked = _round()
    agg = SecureAggregator(IDS, "r1")
    agg.submit("alice", masked["alice"])
    with pytest.raises(ValueError):
        agg.submit("alice", masked["alice"])
    with pytest.raises(ValueError):
        agg.submit("mallory", masked["bob"])
