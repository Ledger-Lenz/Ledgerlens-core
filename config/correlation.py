"""Correlation ID threading and wallet masking utilities."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="unset")

HEADER = "X-Correlation-ID"


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def mask_wallet(addr: str) -> str:
    """Return first-8 + '...' + last-4 characters of a Stellar wallet address."""
    if not addr or len(addr) <= 12:
        return addr
    return f"{addr[:8]}...{addr[-4:]}"


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Read X-Correlation-ID from request (or generate UUID4), set context var,
    and echo it back in the response header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        cid = request.headers.get(HEADER) or str(uuid.uuid4())
        set_correlation_id(cid)
        response = await call_next(request)
        response.headers[HEADER] = cid
        return response
