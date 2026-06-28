import math

from detection.benford_engine import (
    BENFORD_EXPECTED,
    DIGITS,
    _pair_series,
    chi_square_statistic,
    compute_benford_metrics,
    digit_distribution,
    first_digit,
    is_anomalous,
    mean_absolute_deviation,
    z_scores,
)


def test_first_digit_basic():
    assert first_digit(123.45) == 1
    assert first_digit(0.0456) == 4
    assert first_digit(9) == 9


def test_first_digit_edge_cases():
    assert first_digit(1.0) == 1
    assert first_digit(10.0) == 1
    assert first_digit(100.0) == 1
    assert first_digit(0.1) == 1
    assert first_digit(0.01) == 1
    assert first_digit(99.0) == 9
    assert first_digit(999.0) == 9


def test_first_digit_invalid_values():
    assert first_digit(0) is None
    assert first_digit(-5) is None
    assert first_digit(float("nan")) is None
    assert first_digit(float("inf")) is None
    assert first_digit(float("-inf")) is None


def test_benford_expected_distribution_sums_to_one():
    assert math.isclose(sum(BENFORD_EXPECTED.values()), 1.0, rel_tol=1e-9)
    assert BENFORD_EXPECTED[1] > BENFORD_EXPECTED[9]


def test_digit_distribution_empty():
    dist = digit_distribution([])
    assert all(v == 0.0 for v in dist.values())


def test_digit_distribution_all_same_digit():
    dist = digit_distribution([100.0, 100.0, 100.0])
    assert math.isclose(dist[1], 1.0)
    assert all(math.isclose(dist[d], 0.0) for d in range(2, 10))


def test_mean_absolute_deviation_matches_benford_is_zero():
    assert mean_absolute_deviation(BENFORD_EXPECTED) == 0.0


def test_mean_absolute_deviation_extreme_deviation():
    extreme = {d: 0.0 for d in DIGITS}
    extreme[1] = 1.0
    mad = mean_absolute_deviation(extreme)
    assert mad > 0.0
    assert mad > 0.10  # max achievable MAD for first-digit analysis is ~0.155


def test_chi_square_statistic_zero_when_perfect_match():
    chi_sq = chi_square_statistic(BENFORD_EXPECTED, 1000)
    assert chi_sq == 0.0


def test_chi_square_statistic_increases_with_deviation():
    uniform = {d: 1.0 / 9 for d in DIGITS}
    chi_sq_uniform = chi_square_statistic(uniform, 1000)
    chi_sq_benford = chi_square_statistic(BENFORD_EXPECTED, 1000)
    assert chi_sq_uniform > chi_sq_benford


def test_chi_square_statistic_zero_when_no_samples():
    assert chi_square_statistic(BENFORD_EXPECTED, 0) == 0.0


def test_z_scores_zero_when_perfect_match():
    scores = z_scores(BENFORD_EXPECTED, 1000)
    for d in DIGITS:
        assert scores[d] == 0.0


def test_z_scores_positive_when_observed_deviates():
    extreme = {d: 0.0 for d in DIGITS}
    extreme[1] = 1.0
    scores = z_scores(extreme, 1000)
    for d in DIGITS:
        assert scores[d] >= 0.0
    assert scores[1] > 3.0


def test_z_scores_zero_when_no_samples():
    scores = z_scores(BENFORD_EXPECTED, 0)
    for d in DIGITS:
        assert scores[d] == 0.0


def test_compute_benford_metrics_on_round_numbers_is_anomalous():
    amounts = [100.0] * 50 + [200.0] * 5
    metrics = compute_benford_metrics(amounts)

    assert metrics["sample_size"] == 55
    assert is_anomalous(metrics, mad_threshold=0.015)


def test_compute_benford_metrics_on_benford_like_data_is_not_anomalous():
    amounts = []
    for digit, proportion in BENFORD_EXPECTED.items():
        amounts.extend([float(digit)] * round(proportion * 1000))

    metrics = compute_benford_metrics(amounts)
    assert metrics["mad"] < 0.015


def test_pair_series_with_asset_pair_column():
    import pandas as pd
    trades = pd.DataFrame({"asset_pair": ["XLM/USDC", "XLM/BTC"]})
    result = _pair_series(trades)
    assert list(result) == ["XLM/USDC", "XLM/BTC"]


def test_pair_series_derives_from_base_counter():
    import pandas as pd
    trades = pd.DataFrame({
        "base_asset": [{"code": "XLM", "issuer": None}, {"code": "BTC", "issuer": "GXYZ"}],
        "counter_asset": [{"code": "USDC", "issuer": None}, {"code": "ETH", "issuer": None}],
    })
    result = _pair_series(trades)
    assert list(result) == ["XLM/USDC", "BTC:GXYZ/ETH"]
