"""Tests for detection/exceptions.py."""

from detection.exceptions import SubmissionLeaseError


def test_submission_lease_error_is_exception():
    """SubmissionLeaseError should be a standard Exception subclass."""
    assert issubclass(SubmissionLeaseError, Exception)


def test_submission_lease_error_message():
    """SubmissionLeaseError should carry a descriptive message."""
    msg = "Region does not hold the Soroban submission lease."
    exc = SubmissionLeaseError(msg)
    assert str(exc) == msg


def test_submission_lease_error_can_be_raised_and_caught():
    """SubmissionLeaseError should be raiseable and catchable."""
    try:
        raise SubmissionLeaseError("test lease error")
    except SubmissionLeaseError as e:
        assert "test lease error" in str(e)
    else:
        raise AssertionError("Expected SubmissionLeaseError was not raised")
