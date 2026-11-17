"""Caller authentication, session cookie isolation, and canonical Origin validation (v1.6.0).

* ``access_mode == "iap"`` (Cloud Run default for live/record): cryptographically verifies
  ``X-Goog-IAP-JWT-Assertion`` (ES256 signature, ``exp``/``iat``, ``iss``, and ``aud``) via ``google-auth``.
  Missing or invalid assertions fail closed with HTTP 401; raw ``X-Goog-Authenticated-User-Email`` is never trusted.
* ``access_mode == "public"`` (local dev): isolates browser sessions via an ``HttpOnly; SameSite=Lax`` cookie
  (``__Host-rag_arena_sid`` over HTTPS, ``rag_arena_sid`` over HTTP) and ignores raw IAP headers.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests
from starlette.requests import Request

from app.core.config import ArenaSettings

IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_CERTS_TIMEOUT_SECONDS = 5.0
SESSION_COOKIE_HTTP = "rag_arena_sid"
SESSION_COOKIE_HTTPS = "__Host-rag_arena_sid"
_SID_RE = re.compile(r"^sid_[0-9a-f]{32}$")
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_CLOUD_RUN_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9-]{0,62})*\.run\.app$")
_HTTP_SESSION_LOCK = threading.Lock()
_HTTP_SESSION: requests.Session | None = None


class AuthError(ValueError):
    """Caller failed authentication; maps to HTTP 401 JSON at route boundaries."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class _BoundedAuthRequest:
    """``google.auth.transport.Request`` wrapper enforcing a hard socket timeout on IAP key fetches."""

    def __init__(self, session: requests.Session, timeout_seconds: float) -> None:
        self._session = session
        self._timeout = max(1.0, float(timeout_seconds))

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        from google.auth.transport.requests import _Response  # type: ignore[attr-defined]

        eff_timeout = min(self._timeout, float(timeout)) if timeout else self._timeout
        resp = self._session.request(method, url, data=body, headers=headers, timeout=eff_timeout, **kwargs)
        return _Response(resp)


def _shared_session() -> requests.Session:
    global _HTTP_SESSION
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is None:
            _HTTP_SESSION = requests.Session()
        return _HTTP_SESSION


def _decode_jwt_header(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthError("malformed IAP JWT assertion")
    padded = parts[0] + "=" * (-len(parts[0]) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        header = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - untrusted header parse
        raise AuthError("malformed IAP JWT header") from exc
    if not isinstance(header, dict) or header.get("alg") != "ES256":
        raise AuthError("IAP JWT must use alg=ES256")
    return header


def verify_iap_jwt(
    token: str,
    audience: str,
    *,
    certs: Mapping[str, str] | None = None,
    timeout_seconds: float = IAP_CERTS_TIMEOUT_SECONDS,
) -> str:
    """Verify an IAP ES256 JWT and return its authenticated identity (email or sub)."""
    if not isinstance(token, str) or not token.strip():
        raise AuthError("missing X-Goog-IAP-JWT-Assertion header")
    if not isinstance(audience, str) or not audience.strip():
        raise AuthError("IAP_JWT_AUDIENCE is not configured")
    raw_token = token.strip()
    _decode_jwt_header(raw_token)

    try:
        if certs is not None:
            from google.auth import jwt as google_jwt

            claims = google_jwt.decode(raw_token, certs=dict(certs), audience=audience.strip())
        else:
            from google.oauth2 import id_token

            transport = _BoundedAuthRequest(_shared_session(), timeout_seconds)
            claims = id_token.verify_token(
                raw_token,
                transport,
                audience=audience.strip(),
                certs_url=IAP_CERTS_URL,
            )
    except Exception as exc:  # noqa: BLE001 - cryptographic / claim failure -> fail closed
        raise AuthError("invalid or expired IAP JWT assertion") from exc

    if not isinstance(claims, Mapping) or claims.get("iss") != IAP_ISSUER:
        raise AuthError("invalid IAP JWT issuer")
    identity = str(claims.get("email") or claims.get("sub") or "").strip()
    if not identity:
        raise AuthError("IAP JWT assertion carries no subject identity")
    return identity


def _is_https_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def resolve_caller_identity(request: Request, settings: ArenaSettings) -> str:
    """Return the verified caller identity, queuing an HttpOnly session cookie on ``request.state`` in public mode."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    cached = getattr(request.state, "caller_identity", None)
    if isinstance(cached, str) and cached:
        return cached

    if settings.access_mode == "iap":
        certs_override = getattr(request.app.state, "iap_certs_override", None)
        assertion = request.headers.get(IAP_JWT_HEADER, "")
        identity = verify_iap_jwt(assertion, settings.iap_jwt_audience, certs=certs_override)
        request.state.caller_identity = identity
        return identity

    https = _is_https_request(request)
    cookie_name = SESSION_COOKIE_HTTPS if https else SESSION_COOKIE_HTTP
    existing = request.cookies.get(cookie_name, "").strip()
    if not _SID_RE.fullmatch(existing):
        alt = request.cookies.get(SESSION_COOKIE_HTTP, "").strip()
        existing = alt if (not https and _SID_RE.fullmatch(alt)) else ""
    if _SID_RE.fullmatch(existing):
        request.state.caller_identity = existing
        return existing

    fresh = f"sid_{secrets.token_hex(16)}"
    request.state.caller_identity = fresh
    request.state.new_session_cookie = (cookie_name, fresh, https)
    return fresh


def validate_upload_origin(request: Request, settings: ArenaSettings) -> str | None:
    """Validate browser ``Origin`` against ``ALLOWED_UPLOAD_ORIGINS``, Cloud Run ``rag-arena-xxxx-uc.a.run.app``, or localhost."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return None
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or f"{parts.scheme}://{parts.netloc}" != origin:
        raise ValueError("request origin is malformed")
    if settings.allowed_upload_origins:
        if origin in settings.allowed_upload_origins:
            return origin
        raise ValueError("request origin does not match ALLOWED_UPLOAD_ORIGINS")
    if os.environ.get("K_SERVICE"):
        host = request.headers.get("host", "").strip().lower()
        netloc = parts.netloc.lower()
        if parts.scheme == "https" and netloc == host and _CLOUD_RUN_HOST_RE.fullmatch(netloc):
            return origin
        raise ValueError("request origin does not match this Cloud Run service")
    if _LOCAL_ORIGIN_RE.fullmatch(origin):
        return origin
    raise ValueError("request origin does not match this app")


__all__ = [
    "AuthError",
    "IAP_CERTS_URL",
    "IAP_ISSUER",
    "IAP_JWT_HEADER",
    "SESSION_COOKIE_HTTP",
    "SESSION_COOKIE_HTTPS",
    "resolve_caller_identity",
    "validate_upload_origin",
    "verify_iap_jwt",
]
