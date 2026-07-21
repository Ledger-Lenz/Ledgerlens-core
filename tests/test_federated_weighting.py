"""Unit and property-based tests for detection.federated.weighting.

apply_weight_share_cap is the algorithm that actually enforces the per-round
weight-share cap described in docs/federated_learning.md's "Participant
Admission & Weight Bounding" section. These tests exercise it directly and
in isolation (see tests/test_federated_sybil.py for the integration-level
attack-scenario tests that exercise it through the full server).
"""

import numpy as np
import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from detection.federated.weighting import apply_weight_share_cap


# ── Basic / edge cases ────────────────────────────────────────────────────────

def test_empty_input_returns_empty():
    result = apply_weight_share_cap(np.array([]), 0.5)
    assert len(result) == 0


def test_single_entry_is_noop_regardless_of_cap():
    # Capping a lone participant to less than 100% is meaningless -- nothing
    # to redistribute to.
    result = apply_weight_share_cap(np.array([7.0]), 0.1)
    assert np.allclose(result, [1.0])


def test_cap_of_one_is_noop_but_normalizes():
    result = apply_weight_share_cap(np.array([1.0, 2.0, 3.0]), 1.0)
    assert np.allclose(result, [1 / 6, 2 / 6, 3 / 6])
    assert np.isclose(result.sum(), 1.0)


def test_cap_greater_than_one_is_noop():
    result = apply_weight_share_cap(np.array([1.0, 1.0]), 1.5)
    assert np.allclose(result, [0.5, 0.5])


def test_all_zero_weights_returned_unchanged():
    result = apply_weight_share_cap(np.array([0.0, 0.0, 0.0]), 0.5)
    assert np.allclose(result, [0.0, 0.0, 0.0])


def test_equal_weights_under_cap_are_unaffected():
    # 3 equal participants at 1/3 each -- a 0.5 cap should never trigger.
    result = apply_weight_share_cap(np.array([10.0, 10.0, 10.0]), 0.5)
    assert np.allclose(result, [1 / 3, 1 / 3, 1 / 3])


# ── Capping behaviour ──────────────────────────────────────────────────────────

def test_extreme_skew_is_capped_and_remainder_redistributed_proportionally():
    # raw = [1000, 1, 1, 1] -> dominant entry capped at 0.5, remaining 0.5
    # split proportionally among the other three (equal raw weight -> equal share).
    result = apply_weight_share_cap(np.array([1000.0, 1.0, 1.0, 1.0]), 0.5)
    assert np.isclose(result[0], 0.5)
    assert np.allclose(result[1:], 1 / 6)  # 0.5 / 3
    assert np.isclose(result.sum(), 1.0)


def test_cascading_second_entry_becomes_over_cap_after_first_redistribution():
    # After capping the largest entry, the freed weight can push a second
    # entry over the cap too -- the algorithm must iterate, not stop after
    # one pass.
    result = apply_weight_share_cap(np.array([0.6, 0.35, 0.05]), 0.4)
    assert np.allclose(sorted(result), sorted([0.4, 0.4, 0.2]))
    assert np.isclose(result.sum(), 1.0)
    assert np.all(result <= 0.4 + 1e-9)


def test_infeasible_cap_relaxed_to_one_over_n():
    # n=2, cap=0.3: no weight vector summing to 1 can have both entries <= 0.3
    # (their average alone is 0.5). The tightest always-feasible cap is 1/n.
    result = apply_weight_share_cap(np.array([9.0, 1.0]), 0.3)
    assert np.allclose(result, [0.5, 0.5])


def test_infeasible_cap_with_more_participants_relaxes_to_one_over_n():
    result = apply_weight_share_cap(np.array([100.0, 1.0, 1.0, 1.0, 1.0]), 0.05)
    assert np.allclose(result, [0.2] * 5)


def test_raw_weights_need_not_be_pre_normalized():
    # Same relative proportions, different absolute scale -> same result.
    a = apply_weight_share_cap(np.array([1000.0, 1.0, 1.0, 1.0]), 0.5)
    b = apply_weight_share_cap(np.array([1_000_000.0, 1000.0, 1000.0, 1000.0]), 0.5)
    assert np.allclose(a, b)


def test_no_capping_needed_returns_normalized_raw_proportions():
    result = apply_weight_share_cap(np.array([1.0, 2.0, 3.0, 4.0]), 0.9)
    assert np.allclose(result, np.array([1.0, 2.0, 3.0, 4.0]) / 10.0)


# ── Invariants (hand-picked adversarial shapes) ───────────────────────────────

@pytest.mark.parametrize(
    "raw,cap",
    [
        ([1.0], 0.01),
        ([1.0, 1.0], 0.99),
        ([1.0, 1.0], 0.01),
        ([1e9, 1.0, 1.0], 0.3),
        ([1.0, 1.0, 1.0, 1.0, 1.0], 0.19999),  # just under 1/n=0.2
        ([5.0, 4.0, 3.0, 2.0, 1.0], 0.25),
        ([1.0, 0.0, 0.0], 0.5),
    ],
)
def test_invariants_sum_to_one_and_respect_effective_cap(raw, cap):
    raw_arr = np.array(raw)
    n = len(raw_arr)
    result = apply_weight_share_cap(raw_arr, cap)
    assert np.isclose(result.sum(), 1.0, atol=1e-6)
    assert np.all(result >= -1e-9)
    effective_cap = max(cap, 1.0 / n)
    assert np.all(result <= effective_cap + 1e-6)


# ── Property-based test ───────────────────────────────────────────────────────

@given(
    raw=st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=12,
    ),
    cap=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
)
@hyp_settings(max_examples=200)
def test_property_sum_and_cap_invariants_hold(raw, cap):
    raw_arr = np.array(raw)
    if raw_arr.sum() <= 0:
        return  # degenerate all-zero input already covered by a direct test
    n = len(raw_arr)
    result = apply_weight_share_cap(raw_arr, cap)

    assert np.isclose(result.sum(), 1.0, atol=1e-6), (
        f"weights must sum to 1: raw={raw}, cap={cap}, result={result}"
    )
    assert np.all(result >= -1e-9), f"no negative weight: raw={raw}, cap={cap}, result={result}"

    effective_cap = max(cap, 1.0 / n)
    assert np.all(result <= effective_cap + 1e-6), (
        f"no entry may exceed max(cap, 1/n): raw={raw}, cap={cap}, "
        f"effective_cap={effective_cap}, result={result}"
    )

    # An entry that was *not* capped (result strictly below the effective cap)
    # must be at least as large as its raw normalized share -- redistribution
    # only ever adds freed weight to uncapped entries, never removes from them.
    raw_normalized = raw_arr / raw_arr.sum()
    uncapped = result < effective_cap - 1e-6
    assert np.all(result[uncapped] >= raw_normalized[uncapped] - 1e-6), (
        f"uncapped entries must not shrink below their raw share: "
        f"raw_normalized={raw_normalized}, result={result}, uncapped={uncapped}"
    )
