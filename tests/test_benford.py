import math

from detection.benford_engine import (
    BENFORD_EXPECTED,
    compute_benford_metrics,
    digit_distribution,
    first_digit,
    is_anomalous,
    mean_absolute_deviation,
)


def test_first_digit_basic():
    assert first_digit(123.45) == 1
    assert first_digit(0.0456) == 4
    assert first_digit(9) == 9


def test_first_digit_invalid_values():
    assert first_digit(0) is None
    assert first_digit(-5) is None
    assert first_digit(float("nan")) is None


def test_benford_expected_distribution_sums_to_one():
    assert math.isclose(sum(BENFORD_EXPECTED.values()), 1.0, rel_tol=1e-9)
    assert BENFORD_EXPECTED[1] > BENFORD_EXPECTED[9]


def test_digit_distribution_empty():
    dist = digit_distribution([])
    assert all(v == 0.0 for v in dist.values())


def test_mean_absolute_deviation_matches_benford_is_zero():
    assert mean_absolute_deviation(BENFORD_EXPECTED) == 0.0


def test_compute_benford_metrics_on_round_numbers_is_anomalous():
    # Wash-trading-style round amounts concentrate on leading digit 1.
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
