import numpy as np
import pytest
from hypothesis import given, strategies as st

from detection.feature_engineering import (
    _round_trip_count_jit,
    _round_trip_count_python,
    _burst_overlap_count_jit,
    _burst_overlap_count_python,
    round_trip_trade_frequency,
)
from config.settings import settings


@given(
    legs=st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 5)),
        min_size=0, max_size=200,
    ),
    max_trades=st.integers(1, 10),
)
def test_round_trip_jit_matches_python(legs, max_trades):
    gave_ids = np.array([g for g, _ in legs], dtype=np.int64)
    got_ids = np.array([g for _, g in legs], dtype=np.int64)

    jit_result = _round_trip_count_jit(gave_ids, got_ids, max_trades)
    python_result = _round_trip_count_python(legs, max_trades)

    assert jit_result == python_result


@given(
    times_a=st.lists(st.integers(0, 10**6), max_size=100),
    times_b=st.lists(st.integers(0, 10**6), max_size=100),
    window_us=st.integers(1, 10**5),
)
def test_burst_overlap_jit_matches_python(times_a, times_b, window_us):
    a = np.array(times_a, dtype=np.int64)
    b_sorted = np.sort(np.array(times_b, dtype=np.int64))

    jit_result = _burst_overlap_count_jit(a, b_sorted, window_us)
    python_result = _burst_overlap_count_python(a, b_sorted, window_us)

    assert jit_result == python_result


def test_burst_overlap_empty_arrays():
    empty = np.array([], dtype=np.int64)
    assert _burst_overlap_count_jit(empty, empty, 1000) == 0
    assert _burst_overlap_count_python(empty, empty, 1000) == 0


def test_burst_overlap_no_overlap():
    a = np.array([0, 1], dtype=np.int64)
    b = np.array([10**6, 10**6 + 1], dtype=np.int64)
    assert _burst_overlap_count_jit(a, b, 10) == 0


def test_burst_overlap_all_overlapping():
    a = np.array([100, 100, 100], dtype=np.int64)
    b = np.array([100, 100, 100], dtype=np.int64)
    assert _burst_overlap_count_jit(a, b, 10) == 3


def test_jit_disabled_flag_forces_python_path(monkeypatch):
    settings.feature_engine_jit_enabled = False
    trades = _make_trades_df()

    result_flag_off = round_trip_trade_frequency(trades, "acct_0")

    settings.feature_engine_jit_enabled = True
    result_flag_on = round_trip_trade_frequency(trades, "acct_0")

    assert result_flag_off == result_flag_on
    settings.feature_engine_jit_enabled = True  # reset


def test_behaves_identically_without_numba(monkeypatch):
    import detection.feature_engineering as fe_module

    trades = _make_trades_df()
    result_with_numba = round_trip_trade_frequency(trades, "acct_0")

    monkeypatch.setattr(fe_module, "_HAS_NUMBA", False)
    result_without_numba = round_trip_trade_frequency(trades, "acct_0")

    assert result_with_numba == result_without_numba


@pytest.mark.benchmark
def test_jit_is_faster_at_10k_trades():
    import time
    trades = _make_trades_df(n=10_000)

    settings.feature_engine_jit_enabled = True
    round_trip_trade_frequency(trades, "acct_0")  # warm up JIT
    start = time.perf_counter()
    round_trip_trade_frequency(trades, "acct_0")
    jit_time = time.perf_counter() - start

    settings.feature_engine_jit_enabled = False
    start = time.perf_counter()
    round_trip_trade_frequency(trades, "acct_0")
    python_time = time.perf_counter() - start

    settings.feature_engine_jit_enabled = True
    print(f"JIT: {jit_time:.4f}s, Python: {python_time:.4f}s, speedup: {python_time/jit_time:.1f}x")
    assert jit_time < python_time  # documented, not a hard 3x CI gate


def _make_trades_df(n: int = 500):
    import pandas as pd
    rng = np.random.default_rng(0)
    symbols = [f"ASSET{i}" for i in range(10)]
    return pd.DataFrame({
        "account": ["acct_0"] * n,
        "gave": rng.choice(symbols, n),
        "got": rng.choice(symbols, n),
        "pair": rng.choice(["PAIR0", "PAIR1"], n),
        "timestamp_us": np.sort(rng.integers(0, 10**6, n)).astype(np.int64),
    })