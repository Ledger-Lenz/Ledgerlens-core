import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config.settings import settings
from config.correlation import mask_wallet

logger = logging.getLogger("ledgerlens.waf")

# SQL injection patterns
_SQLI_PATTERNS = [
    re.compile(r"(?i)\b(union\s+select)\b", re.IGNORECASE),
    re.compile(r"(?i)\b(drop\s+table)\b", re.IGNORECASE),
    re.compile(r";\s*--", re.IGNORECASE),
    re.compile(r"(?i)\b(or\s+1\s*=\s*1)\b", re.IGNORECASE),
]

# XSS patterns
_XSS_PATTERNS = [
    re.compile(r"(?i)<script[^>]*>", re.IGNORECASE),
    re.compile(r"(?i)javascript:", re.IGNORECASE),
    re.compile(r"(?i)onerror\s*=", re.IGNORECASE),
]

# In-memory store for blocked requests (kept for admin inspection)
_BLOCKED_REQUESTS = deque(maxlen=1000)


class WAFMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Callable,
        max_body_bytes: Optional[int] = None,
        slow_request_timeout_seconds: Optional[float] = None,
    ):
        super().__init__(app)
        self.max_body_bytes = max_body_bytes or settings.waf_max_body_bytes
        self.slow_request_timeout = slow_request_timeout_seconds or settings.waf_slow_request_timeout_seconds

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not settings.waf_enabled:
            return await call_next(request)

        try:
            # Check for oversized body early (before reading)
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.max_body_bytes:
                return self._block_request(request, "oversized_body", "Request body too large")

            # Check query parameters for malicious patterns
            query_params = dict(request.query_params)
            query_match = self._scan_payload(query_params)
            if query_match:
                return self._block_request(request, "query_injection", query_match)

            # Read and check request body
            body = await self._safe_read_body(request)
            if body is None:
                return self._block_request(request, "slow_request", "Request timed out")

            # If body is JSON, scan it
            if body and request.headers.get("content-type", "").startswith("application/json"):
                try:
                    json_body = json.loads(body)
                    json_match = self._scan_payload(json_body)
                    if json_match:
                        return self._block_request(request, "body_injection", json_match)
                except json.JSONDecodeError:
                    pass  # Not valid JSON, pass through

            # Replace request body so it can be read again by downstream
            async def mock_receive():
                return {"type": "http.request", "body": body or b""}

            request._receive = mock_receive
            return await call_next(request)

        except Exception as e:
            logger.error("WAF middleware error, failing open: %s", e)
            return await call_next(request)

    async def _safe_read_body(self, request: Request) -> Optional[bytes]:
        """Read body with timeout to prevent slowloris attacks."""
        try:
            body = b""
            remaining = self.max_body_bytes
            start_time = time.monotonic()

            while True:
                if time.monotonic() - start_time > self.slow_request_timeout:
                    return None

                message = await request.receive()
                if message["type"] == "http.request":
                    chunk = message.get("body", b"")
                    if len(chunk) > remaining:
                        return None  # Oversized
                    body += chunk
                    remaining -= len(chunk)
                    if not message.get("more_body"):
                        return body
        except Exception:
            return None

    def _scan_payload(self, payload: dict | list | str) -> Optional[str]:
        """Scan a payload for malicious patterns. Returns rule name if match found."""
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(key, str):
                    match = self._scan_string(key)
                    if match:
                        return match
                result = self._scan_payload(value)
                if result:
                    return result
        elif isinstance(payload, list):
            for item in payload:
                result = self._scan_payload(item)
                if result:
                    return result
        elif isinstance(payload, str):
            return self._scan_string(payload)
        return None

    def _scan_string(self, s: str) -> Optional[str]:
        """Scan a single string for patterns."""
        for pattern in _SQLI_PATTERNS:
            if pattern.search(s):
                return "sql_injection"
        for pattern in _XSS_PATTERNS:
            if pattern.search(s):
                return "xss"
        return None

    def _block_request(self, request: Request, rule: str, reason: str) -> JSONResponse:
        """Block a request and log it."""
        blocked_at = datetime.now(timezone.utc).isoformat()
        # Mask wallet addresses in the request URL and headers
        masked_url = mask_wallet(str(request.url))
        masked_headers = {k: mask_wallet(v) for k, v in request.headers.items()}

        # Get namespace_id from API key if available
        namespace_id = ""
        try:
            from detection.api_key_store import lookup_key
            api_key = request.headers.get("X-LedgerLens-Api-Key")
            if api_key:
                key_data = lookup_key(api_key)
                if key_data:
                    namespace_id = key_data.get("namespace_id", "")
        except Exception:
            pass

        blocked_request = {
            "timestamp": blocked_at,
            "rule": rule,
            "reason": reason,
            "method": request.method,
            "url": masked_url,
            "headers": masked_headers,
            "namespace_id": namespace_id,
        }
        _BLOCKED_REQUESTS.append(blocked_request)

        # Increment Prometheus counter if available
        try:
            from api.metrics import ledgerlens_waf_blocks_total
            ledgerlens_waf_blocks_total.labels(rule=rule, namespace_id=namespace_id).inc()
        except Exception:
            pass

        logger.warning("WAF blocked request: rule=%s, url=%s, namespace=%s", rule, masked_url, namespace_id)
        status_code = 413 if rule == "oversized_body" else 400
        return JSONResponse(status_code=status_code, content={"detail": "Bad request"})


def get_blocked_requests(limit: int = 100) -> list[dict]:
    """Return recent blocked requests (for admin API)."""
    return list(_BLOCKED_REQUESTS)[-limit:]
