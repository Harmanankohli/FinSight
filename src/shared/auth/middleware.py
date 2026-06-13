"""ASGI auth middleware — gates HTTP requests, passes lifespan/websocket through.

EC2: public paths matched via ``path.rstrip("/")`` so ``/health/`` == ``/health``.
EC3: non-http scopes (lifespan) pass through untouched.
E4: empty/short bearer credentials rejected before any comparison.
G10: SSE rejected BEFORE the stream starts — 401 JSON envelope, never a stream.
"""

from __future__ import annotations

import json
import logging
from hmac import compare_digest
from typing import Any

from starlette.middleware import Middleware

from shared.auth.tokens import AuthError, Principal, verify_user_token
from shared.settings import get_settings

logger = logging.getLogger(__name__)

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/card",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
    }
)

PUBLIC_PREFIXES: tuple[str, ...] = (
    "/.well-known/",
    "/api/agents",
    "/api/reports",
)


def _is_public(path: str) -> bool:
    cleaned = path.rstrip("/")
    if cleaned in PUBLIC_PATHS:
        return True
    if any(cleaned.startswith(p) for p in PUBLIC_PREFIXES):
        return True
    return False


class _AuthUser:
    """Minimal user-like object for Starlette scope["user"] bridge.

    A2A SDK's StarletteUser reads request.user.is_authenticated and
    request.user.display_name from the Starlette Request's scope["user"].
    """

    is_authenticated = True
    display_name = ""

    def __init__(self, display_name: str):
        self.display_name = display_name


def _json_error(code: str, message: str, status: int = 401) -> bytes:
    body = json.dumps({"error": {"code": code, "message": message}})
    return body.encode("utf-8")


def _parse_auth_header(auth_header: bytes | None) -> str | None:
    """Extract bearer token from an ASGI Authorization header.

    Returns None if missing or not a bearer scheme.
    """
    if not auth_header:
        return None
    try:
        raw = auth_header.decode("latin-1").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    if not raw:
        return None
    # Case-insensitive scheme matching
    space = raw.find(" ")
    if space == -1:
        return None
    scheme = raw[:space].lower()
    if scheme != "bearer":
        return None
    cred = raw[space + 1 :].strip()
    return cred if cred else None


def _verify_service_token(token: str) -> bool:
    """Constant-time comparison against the configured service token.

    E4: tokens shorter than 16 chars are rejected before comparison.
    """
    s = get_settings()
    if len(token) < 16:
        return False
    expected = s.service_auth_token
    if len(expected) < 16:
        return False
    return compare_digest(token, expected)


class AuthMiddleware:
    """ASGI middleware for auth enforcement.

    Handles ``scope["type"] == "http"`` only. Lifespan and websocket
    scopes pass through untouched (EC3).

    Sets ``scope["finsight.principal"]`` on success.
    """

    def __init__(
        self,
        app: Any,
        *,
        accept: frozenset[str] | None = None,
    ) -> None:
        self.app = app
        # accept ⊆ {"user", "service"}; default: both
        self.accept = accept or frozenset({"user", "service"})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # EC2: strip trailing slash for path matching
        path = scope.get("path", "/").rstrip("/") or "/"

        if _is_public(path):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []) or [])
        auth_header = headers.get(b"authorization")

        token = _parse_auth_header(auth_header)

        if not token:
            logger.info("auth.denied reason=missing_header path=%s", path)
            await self._send_json(
                send, 401, "UNAUTHENTICATED", "Missing or malformed Authorization header"
            )
            return

        # E4: reject short/empty tokens before any comparison
        if len(token) < 16:
            logger.info("auth.denied reason=short_token path=%s len=%d", path, len(token))
            await self._send_json(send, 401, "UNAUTHENTICATED", "Invalid credentials")
            return

        # Try service token first
        if "service" in self.accept and _verify_service_token(token):
            principal = Principal("service", "mcp-server")
            scope["finsight.principal"] = principal
            scope["user"] = _AuthUser(principal.subject)
            await self.app(scope, receive, send)
            return

        # Try user JWT
        if "user" in self.accept:
            try:
                principal = verify_user_token(token)
                scope["finsight.principal"] = principal
                scope["user"] = _AuthUser(principal.subject)
                await self.app(scope, receive, send)
                return
            except AuthError:
                pass

        # Neither matched
        if "service" in self.accept and "user" not in self.accept:
            logger.info("auth.denied reason=wrong_kind path=%s expected=service", path)
            await self._send_json(send, 403, "FORBIDDEN", "Service authentication required")
        elif "user" in self.accept and "service" not in self.accept:
            logger.info("auth.denied reason=invalid_token path=%s", path)
            await self._send_json(send, 401, "UNAUTHENTICATED", "Invalid or expired token")
        else:
            logger.info("auth.denied reason=no_match path=%s", path)
            await self._send_json(send, 401, "UNAUTHENTICATED", "Invalid or expired token")

    async def _send_json(self, send: Any, status: int, code: str, message: str) -> None:
        body = _json_error(code, message, status)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )


# ── Helpers ─────────────────────────────────────────────────────────────────────


def get_principal(scope_or_request: Any) -> Principal | None:
    """Extract the Principal from an ASGI scope or Starlette Request.

    Returns None if not authenticated.
    """
    scope = (
        scope_or_request
        if isinstance(scope_or_request, dict)
        else getattr(scope_or_request, "scope", None)
    )
    if not scope:
        return None
    return scope.get("finsight.principal")


def require(
    scope_or_request: Any,
    *,
    kinds: tuple[str, ...] = ("user",),
    role: str | None = None,
) -> Principal:
    """Require a principal matching given kinds and optional role.

    Raises AuthError with appropriate code on failure.
    """
    p = get_principal(scope_or_request)
    if p is None:
        raise AuthError("UNAUTHENTICATED")
    if p.kind not in kinds:
        raise AuthError("FORBIDDEN")
    if role is not None and p.role != role:
        raise AuthError("FORBIDDEN")
    return p


def build_auth_middleware(
    settings: Any,
    accept: frozenset[str] | None = None,
) -> list:
    """Build the middleware list for a Starlette app.

    Returns an empty list when auth is disabled (preserves current behaviour).
    When enabled, returns a single Middleware(AuthMiddleware, ...) entry.
    Accept can be used to restrict to user-only or service-only routes.
    """
    if not settings.auth_enabled:
        return []
    return [Middleware(AuthMiddleware, accept=accept)]
