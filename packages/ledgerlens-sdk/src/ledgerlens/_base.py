"""Shared request-building / error-handling helpers for the sync and async clients."""

from __future__ import annotations

import httpx

from .exceptions import LedgerLensAPIError

DEFAULT_TIMEOUT = 10.0
ADMIN_KEY_HEADER = "X-LedgerLens-Admin-Key"


def build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        # Sent on every request; only admin-gated endpoints (e.g. /feedback)
        # actually check it -- harmless on public endpoints.
        headers[ADMIN_KEY_HEADER] = api_key
    return headers


def clean_params(params: dict | None) -> dict | None:
    """Drop `None`-valued query params rather than sending them as the
    literal string "None"."""
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def raise_for_status(response: httpx.Response) -> None:
    """Raise `LedgerLensAPIError` for any non-2xx response."""
    if response.is_success:
        return
    detail = response.text
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            detail = str(body["detail"])
    except ValueError:
        pass
    raise LedgerLensAPIError(response.status_code, detail, response.text)
