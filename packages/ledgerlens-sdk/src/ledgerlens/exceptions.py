"""Exceptions raised by the LedgerLens SDK."""

from __future__ import annotations


class LedgerLensError(Exception):
    """Base class for every exception raised by this SDK."""


class LedgerLensAPIError(LedgerLensError):
    """Raised when the LedgerLens API returns a non-2xx response.

    Attributes
    ----------
    status_code:
        The HTTP status code returned by the server.
    detail:
        The parsed `detail` field from the error response body, if the
        body was JSON and had one; otherwise the raw response text.
    response_body:
        The raw response body text, for debugging.
    """

    def __init__(self, status_code: int, detail: str, response_body: str = ""):
        self.status_code = status_code
        self.detail = detail
        self.response_body = response_body
        super().__init__(f"LedgerLens API error {status_code}: {detail}")
