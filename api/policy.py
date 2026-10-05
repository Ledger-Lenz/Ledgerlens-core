"""Unified policy-enforcement layer shared by every protocol surface (#969).

REST (:mod:`api.gateway`), GraphQL (:mod:`api.graphql_schema`), gRPC
(:mod:`api.grpc_scoring_service`) and WebSocket (:mod:`api.ws_router`) all
resolve credentials, check scopes and enforce rate limits / quotas through
:func:`enforce`, so equivalent requests get identical decisions regardless of
protocol. Each protocol only maps :class:`PolicyDecision.status` onto its own
wire-level error (HTTP status, ``GraphQLError``, gRPC status code, WS close).

Also owns the runtime-configurable API-key tier table (#968) and the
key-cycling abuse detector. See ``docs/api_policy.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from config.settings import settings

logger = logging.getLogger("ledgerlens.policy")
security_logger = logging.getLogger("ledgerlens.security")

# ---------------------------------------------------------------------------
# Security alerting
# ---------------------------------------------------------------------------

#: Callables invoked as ``hook(event, details)`` for every security alert.
#: Integrations (PagerDuty, Slack, SIEM) register here; failures are swallowed.
SECURITY_ALERT_HOOKS: list[Callable[[str, dict], None]] = []


def emit_security_alert(event: str, **details) -> None:
    """Log a CRITICAL security event and fan it out to registered hooks."""
    security_logger.critical("security_alert event=%s details=%s", event, details)
    for hook in SECURITY_ALERT_HOOKS:
        try:
            hook(event, details)
        except Exception:  # pragma: no cover - alerting must never break requests
            logger.exception("security alert hook failed for event=%s", event)


# ---------------------------------------------------------------------------
# Tier configuration (runtime-reloadable)
# ---------------------------------------------------------------------------

#: ``requests_per_minute`` / ``graphql_max_cost`` of 0 means unlimited.
DEFAULT_TIER_LIMITS: dict[str, dict[str, int]] = {
    "anonymous": {"requests_per_minute": 30, "graphql_max_cost": 100},
    "free": {"requests_per_minute": 60, "graphql_max_cost": 250},
    "standard": {"requests_per_minute": 300, "graphql_max_cost": 1000},
    "enterprise": {"requests_per_minute": 1200, "graphql_max_cost": 5000},
    "admin": {"requests_per_minute": 0, "graphql_max_cost": 0},
}

TIER_LIMITS_FILE_ENV = "LEDGERLENS_TIER_LIMITS_FILE"

_tier_lock = threading.Lock()
_tier_overrides: dict[str, dict[str, int]] = {}
_tier_file_cache: tuple[str, float, dict] = ("", 0.0, {})


def _load_tier_file() -> dict:
    """Read the JSON tier file named by ``LEDGERLENS_TIER_LIMITS_FILE``.

    Re-read whenever its mtime changes so operators can retune limits without
    a redeploy. A malformed file keeps the last good value.
    """
    global _tier_file_cache
    path = os.environ.get(TIER_LIMITS_FILE_ENV, "")
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached_path, cached_mtime, cached = _tier_file_cache
    if cached_path == path and cached_mtime == mtime:
        return cached
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise TypeError("tier limits file must contain a JSON object")
    except (OSError, TypeError, ValueError) as exc:
        logger.error("Ignoring invalid tier limits file %s: %s", path, exc)
        return cached if cached_path == path else {}
    _tier_file_cache = (path, mtime, data)
    return data


def set_tier_limits(overrides: dict[str, dict[str, int]]) -> None:
    """Apply in-process tier overrides at runtime (e.g. from an admin endpoint)."""
    with _tier_lock:
        _tier_overrides.clear()
        _tier_overrides.update({k: dict(v) for k, v in overrides.items()})


def get_tier_limits(tier: str) -> dict[str, int]:
    """Effective limits for *tier*: defaults < tier file < runtime overrides."""
    base = DEFAULT_TIER_LIMITS.get(tier, DEFAULT_TIER_LIMITS["free"])
    merged = dict(base)
    merged.update(_load_tier_file().get(tier, {}))
    with _tier_lock:
        merged.update(_tier_overrides.get(tier, {}))
    return merged


def tier_for(key_meta: dict | None) -> str:
    """Tier of a resolved credential; unauthenticated callers are ``anonymous``."""
    if key_meta is None:
        return "anonymous"
    if key_meta.get("auth_type") in ("admin_key", "compliance_key"):
        return "admin"
    return key_meta.get("tier") or "free"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def resolve_credentials(
    admin_key: str = "", api_key: str = "", compliance_key: str = ""
) -> dict | None:
    """Resolve raw credentials to key metadata, or None when none is valid.

    Order: admin key, compliance key, scoped API key (canonical store).
    """
    from detection.api_key_store import get_api_key_by_hash

    if (
        admin_key
        and settings.admin_api_key
        and secrets.compare_digest(admin_key, settings.admin_api_key)
    ):
        return {
            "key_id": "__admin__",
            "key_hash": "",
            "namespace_id": "*",
            "scopes": "admin",
            "rate_limit_per_minute": 0,
            "daily_quota": 0,
            "namespace_daily_quota": 0,
            "auth_type": "admin_key",
        }

    if (
        compliance_key
        and settings.compliance_api_key
        and secrets.compare_digest(compliance_key, settings.compliance_api_key)
    ):
        return {
            "key_id": "__compliance__",
            "key_hash": "",
            "namespace_id": "*",
            "scopes": "compliance:read",
            "rate_limit_per_minute": 0,
            "daily_quota": 0,
            "namespace_daily_quota": 0,
            "auth_type": "compliance_key",
        }

    if not api_key:
        return None
    key_hash = hashlib.blake2b(api_key.encode(), digest_size=32).hexdigest()
    record = get_api_key_by_hash(key_hash)
    if record is None:
        return None
    record["auth_type"] = "api_key"
    return record


def check_scope(required_scope: str | None, key_meta: dict) -> bool:
    """True when *key_meta* carries *required_scope* (``admin`` grants all)."""
    if required_scope is None:
        return True
    scopes = {s.strip() for s in key_meta.get("scopes", "").split(",") if s.strip()}
    return required_scope in scopes or "admin" in scopes


def check_quota(key_meta: dict) -> tuple[bool, dict]:
    """Enforce per-key and per-namespace quota (per-minute, daily, monthly).

    Returns (allowed, headers_dict) — headers carry Retry-After / quota reset.
    """
    from detection.api_key_store import (
        check_daily_quota,
        check_monthly_quota,
        check_namespace_monthly_quota,
        check_namespace_quota,
        check_rate_limit,
    )

    key_id = key_meta["key_id"]
    namespace_id = key_meta.get("namespace_id", "")

    rate_limit = key_meta.get("rate_limit_per_minute", 0)
    if rate_limit > 0:
        allowed, retry_after = check_rate_limit(key_id, rate_limit)
        if not allowed:
            return False, {"Retry-After": str(retry_after)}

    daily_limit = key_meta.get("daily_quota", 0)
    if daily_limit > 0:
        allowed, reset_at = check_daily_quota(key_id, daily_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    ns_daily_limit = key_meta.get("namespace_daily_quota", 0)
    if ns_daily_limit > 0 and namespace_id != "*":
        allowed, reset_at = check_namespace_quota(namespace_id, ns_daily_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    monthly_limit = key_meta.get("monthly_quota", 0)
    if monthly_limit > 0:
        allowed, reset_at = check_monthly_quota(key_id, monthly_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    ns_monthly_limit = key_meta.get("namespace_monthly_quota", 0)
    if ns_monthly_limit > 0 and namespace_id != "*":
        allowed, reset_at = check_namespace_monthly_quota(namespace_id, ns_monthly_limit)
        if not allowed:
            return False, {"X-LedgerLens-Quota-Reset": reset_at}

    return True, {}


# ---------------------------------------------------------------------------
# Tier-aware rate limiting (#968)
# ---------------------------------------------------------------------------


def rate_limit_bucket(key_meta: dict | None, client_ip: str) -> str:
    """Bucket id: authenticated traffic by key, anonymous traffic by IP."""
    if key_meta is not None:
        return f"key:{key_meta['key_id']}"
    return f"ip:{client_ip or 'unknown'}"


def check_tier_rate_limit(key_meta: dict | None, client_ip: str) -> tuple[bool, int, str]:
    """Apply the per-minute limit for the caller's tier.

    Returns (allowed, retry_after_seconds, tier).
    """
    from detection.api_key_store import check_rate_limit

    tier = tier_for(key_meta)
    limit = get_tier_limits(tier).get("requests_per_minute", 0)
    if limit <= 0:
        return True, 0, tier
    allowed, retry_after = check_rate_limit(rate_limit_bucket(key_meta, client_ip), limit)
    return allowed, retry_after, tier


def _ip_range(client_ip: str) -> str:
    """Collapse an address to its /24 (IPv4) or /48 (IPv6) range."""
    if ":" in client_ip:
        return ":".join(client_ip.split(":")[:3]) + "::/48"
    parts = client_ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0/24"
    return client_ip or "unknown"


class KeyCyclingDetector:
    """Flag an IP range presenting many distinct API keys in a short window.

    Key-cycling (rotating through leaked/trial keys from one source to dodge
    per-key limits) shows up as many distinct key fingerprints per range.
    """

    def __init__(self, threshold: int = 10, window_seconds: float = 60.0) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._seen: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self._alerted: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, client_ip: str, api_key: str, now: float | None = None) -> bool:
        """Record one key presentation; return True if the range is abusive."""
        if not api_key:
            return False
        now = time.monotonic() if now is None else now
        ip_range = _ip_range(client_ip)
        fingerprint = hashlib.blake2b(api_key.encode(), digest_size=8).hexdigest()
        with self._lock:
            window = self._seen[ip_range]
            window.append((now, fingerprint))
            while window and now - window[0][0] > self.window_seconds:
                window.popleft()
            distinct = len({fp for _, fp in window})
            if distinct < self.threshold:
                return False
            should_alert = now - self._alerted.get(ip_range, -1e18) > self.window_seconds
            if should_alert:
                self._alerted[ip_range] = now
        if should_alert:
            emit_security_alert(
                "api_key_cycling", ip_range=ip_range, distinct_keys=distinct,
                window_seconds=self.window_seconds,
            )
        return True

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._alerted.clear()


key_cycling_detector = KeyCyclingDetector()


# ---------------------------------------------------------------------------
# Single enforcement entry point
# ---------------------------------------------------------------------------

OK = "ok"
UNAUTHENTICATED = "unauthenticated"
FORBIDDEN = "forbidden"
RATE_LIMITED = "rate_limited"


@dataclass
class PolicyDecision:
    status: str
    detail: str = ""
    key_meta: dict | None = None
    headers: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.status == OK


def enforce(
    required_scope: str | None,
    *,
    admin_key: str = "",
    api_key: str = "",
    compliance_key: str = "",
) -> PolicyDecision:
    """Authenticate, authorise and rate-limit one request. Used by every protocol."""
    if required_scope is None:
        return PolicyDecision(OK)

    key_meta = resolve_credentials(admin_key, api_key, compliance_key)
    if key_meta is None:
        return PolicyDecision(
            UNAUTHENTICATED,
            "Unauthorized — provide a valid API key, admin key, or compliance key",
        )
    if not check_scope(required_scope, key_meta):
        return PolicyDecision(FORBIDDEN, f"Scope '{required_scope}' required", key_meta)

    allowed, headers = check_quota(key_meta)
    if not allowed:
        return PolicyDecision(RATE_LIMITED, "Quota exceeded", key_meta, headers)
    return PolicyDecision(OK, key_meta=key_meta)
