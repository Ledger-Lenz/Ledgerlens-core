"""Detection-specific exception classes used across the ledgerlens pipeline."""


class SubmissionLeaseError(Exception):
    """Raised when this region does not currently hold the Soroban submission lease."""
