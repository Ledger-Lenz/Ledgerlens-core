"""Authentication and authorisation dependencies for LedgerLens API endpoints.

.. deprecated::
    ``api/auth.py`` is maintained for backward compatibility during the
    gateway transition (see ``docs/api_gateway.md``). New code should rely
    on :class:`api.gateway.GatewayMiddleware` instead.

Provides two dependency factories that delegate to the consolidated
:mod:`api.gateway` module:

- :func:`require_admin_key` — delegates to gateway admin-key check.
- :func:`require_compliance_key` — delegates to gateway compliance-key check.

Also provides :class:`TokenService` — short-lived, audience-scoped JWT access
tokens with rotating refresh tokens and reuse detection (#967): presenting a
refresh token that was already rotated out revokes its whole token family
and raises a ``refresh_token_reuse`` security alert.

.. note::
    A third dependency, ``require_api_key_scope``, previously lived here and
    duplicated the scoped-API-key + rate-limit checks now owned by
    :func:`api.api_key_router.require_scope` / :class:`api.gateway.GatewayMiddleware`.
    It was never imported by any router (dead code) and was independently
    broken — it referenced three functions (``_check_rate_limit_redis``,
    ``_check_rate_limit_local``, ``_rate_check``) that were never defined
    anywhere, so calling it would have raised ``NameError``. It was removed
    rather than fixed: fixing it would have meant standing up a *second*,
    parallel rate-limit enforcement path when the actual fix (making
    ``detection.api_key_store.check_rate_limit`` itself distributed, see
    ``detection/rate_limiter.py``) already covers every real call site.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import Header, HTTPException

from api.policy import emit_security_alert
from config.settings import settings

#: API surfaces a token can be scoped to.
AUDIENCES = frozenset({"rest", "graphql", "grpc", "ws"})

# ---------------------------------------------------------------------------
# Backward-compatible single-key auth (delegates to gateway)
# ---------------------------------------------------------------------------


def require_admin_key(x_ledgerlens_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency gating admin-only endpoints (backward compatible).

    Delegates to the gateway's admin-key resolution. Fails closed.
    """
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin API key is not configured")

    if not x_ledgerlens_admin_key:
        raise HTTPException(status_code=401, detail="Missing X-LedgerLens-Admin-Key header")

    if not secrets.compare_digest(x_ledgerlens_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=403, detail="Invalid admin key")


def require_compliance_key(x_ledgerlens_compliance_key: str = Header(default="")) -> None:
    """FastAPI dependency gating compliance endpoints (backward compatible).

    Delegates to the gateway's compliance-key resolution. Fails closed.
    """
    if not settings.compliance_api_key:
        raise HTTPException(status_code=503, detail="Compliance API key is not configured")

    if not x_ledgerlens_compliance_key or not secrets.compare_digest(
        x_ledgerlens_compliance_key, settings.compliance_api_key
    ):
        raise HTTPException(status_code=403, detail="Missing or invalid compliance:read scope")


# ---------------------------------------------------------------------------
# Short-lived, audience-scoped JWTs with refresh-token rotation (#967)
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised for any invalid, expired, mis-scoped or revoked token."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@dataclass
class _Family:
    subject: str
    audiences: list[str]
    scopes: str
    current_jti: str
    expires_at: float
    used_jtis: set[str] = field(default_factory=set)
    revoked: bool = False


class TokenService:
    """Issue and verify HS256 JWTs; rotate refresh tokens with reuse detection.

    Every refresh token belongs to a *family* started at login. Refreshing
    consumes the presented token and issues its successor; presenting an
    already-consumed token means it was copied, so the whole family is
    revoked (legitimate client and attacker both lose access) and an alert
    is emitted.
    """

    def __init__(
        self,
        signing_key: str | None = None,
        access_ttl: int | None = None,
        refresh_ttl: int | None = None,
        issuer: str | None = None,
    ) -> None:
        key = signing_key or settings.jwt_signing_key or settings.service_secret_key
        if not key:
            raise RuntimeError("JWT signing key is not configured (LEDGERLENS_JWT_SIGNING_KEY)")
        self._key = key.encode()
        self.access_ttl = access_ttl or settings.jwt_access_ttl_seconds
        self.refresh_ttl = refresh_ttl or settings.jwt_refresh_ttl_seconds
        self.issuer = issuer or settings.jwt_issuer
        self._families: dict[str, _Family] = {}
        self._lock = threading.Lock()

    # -- JWT encoding --------------------------------------------------------

    def _encode(self, claims: dict) -> str:
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = hmac.new(self._key, f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{_b64(sig)}"

    def _decode(self, token: str, typ: str) -> dict:
        try:
            header, payload, sig = token.split(".")
            expected = hmac.new(self._key, f"{header}.{payload}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(sig)):
                raise TokenError("invalid signature")
            if json.loads(_unb64(header)).get("alg") != "HS256":
                raise TokenError("unsupported algorithm")
            claims = json.loads(_unb64(payload))
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError("malformed token") from exc
        if claims.get("typ") != typ:
            raise TokenError(f"expected a {typ} token")
        if claims.get("iss") != self.issuer:
            raise TokenError("invalid issuer")
        if claims.get("exp", 0) <= time.time():
            raise TokenError("token expired")
        return claims

    # -- Public API ----------------------------------------------------------

    def _access_token(self, subject: str, audience: str, scopes: str, family_id: str) -> str:
        if audience not in AUDIENCES:
            raise TokenError(f"unknown audience '{audience}'")
        now = int(time.time())
        return self._encode({
            "iss": self.issuer, "sub": subject, "aud": audience, "scope": scopes,
            "fam": family_id, "typ": "access", "iat": now, "exp": now + self.access_ttl,
            "jti": uuid.uuid4().hex,
        })

    def _refresh_token(self, family: _Family, family_id: str) -> str:
        now = int(time.time())
        return self._encode({
            "iss": self.issuer, "sub": family.subject, "fam": family_id, "typ": "refresh",
            "iat": now, "exp": int(family.expires_at), "jti": family.current_jti,
        })

    def issue(self, subject: str, audiences: list[str], scopes: str, audience: str) -> dict:
        """Start a new token family; return an access token for *audience* + refresh token."""
        if audience not in audiences or not set(audiences) <= AUDIENCES:
            raise TokenError("audience not permitted for this client")
        family_id = uuid.uuid4().hex
        family = _Family(subject, list(audiences), scopes, uuid.uuid4().hex,
                         time.time() + self.refresh_ttl)
        with self._lock:
            self._families[family_id] = family
        return {
            "access_token": self._access_token(subject, audience, scopes, family_id),
            "refresh_token": self._refresh_token(family, family_id),
            "expires_in": self.access_ttl,
        }

    def refresh(self, refresh_token: str, audience: str) -> dict:
        """Rotate *refresh_token*; reuse of a rotated-out token revokes the family."""
        claims = self._decode(refresh_token, "refresh")
        family_id, jti = claims.get("fam", ""), claims.get("jti", "")
        with self._lock:
            family = self._families.get(family_id)
            if family is None or family.revoked:
                raise TokenError("token family revoked")
            if jti != family.current_jti:
                reused = jti in family.used_jtis
                family.revoked = True
                subject = family.subject
            else:
                reused = None
                if audience not in family.audiences:
                    raise TokenError("audience not permitted for this client")
                family.used_jtis.add(jti)
                family.current_jti = uuid.uuid4().hex
        if reused is not None:
            emit_security_alert(
                "refresh_token_reuse", subject=subject, family_id=family_id, known_jti=reused,
            )
            raise TokenError("refresh token reuse detected; token family revoked")
        return {
            "access_token": self._access_token(family.subject, audience, family.scopes, family_id),
            "refresh_token": self._refresh_token(family, family_id),
            "expires_in": self.access_ttl,
        }

    def verify_access(self, token: str, audience: str) -> dict:
        """Verify an access token for *audience*; rejects tokens of revoked families."""
        claims = self._decode(token, "access")
        if claims.get("aud") != audience:
            raise TokenError("token not valid for this audience")
        with self._lock:
            family = self._families.get(claims.get("fam", ""))
            if family is None or family.revoked:
                raise TokenError("token family revoked")
        return claims

    def revoke_family(self, family_id: str) -> None:
        with self._lock:
            family = self._families.get(family_id)
            if family is not None:
                family.revoked = True
