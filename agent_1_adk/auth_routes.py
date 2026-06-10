"""Authentication REST routes — login, refresh, logout, me.

Boundary A: user → frontend → orchestrator.
Depends on shared.auth.tokens and shared.memory.user_store.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.auth.audit import log_lockout, log_login, log_refresh
from shared.auth.middleware import require
from shared.auth.tokens import (
    AuthError,
    decode_refresh_token,
    issue_refresh_token,
    issue_user_token,
)
from shared.memory.user_store import (
    get_refresh_token,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    store_refresh_token,
    update_rotated_at,
    verify_password,
)
from shared.rate_limiter import TokenBucket
from shared.settings import get_settings

logger = logging.getLogger(__name__)

# Login lockout: per username+IP, 15 min window
_login_buckets: dict[str, TokenBucket] = {}
_login_lock = asyncio.Lock()


async def _check_lockout(username: str, ip: str) -> bool:
    """Return True if this username+IP is locked out."""
    key = f"{username}:{ip}"
    async with _login_lock:
        bucket = _login_buckets.get(key)
        if bucket is None:
            s = get_settings()
            bucket = TokenBucket(rate=s.login_max_attempts / 900.0, burst=s.login_max_attempts)
            _login_buckets[key] = bucket
        return not await bucket.try_acquire()


_ERROR_ENVELOPE = lambda code, msg, status=400: JSONResponse(  # noqa: E731
    {"error": {"code": code, "message": msg}}, status_code=status
)


def _client_ip(request: Request) -> str:
    """Extract client IP, honoring X-Forwarded-For only from trusted proxies (EC5).

    If TRUSTED_PROXIES is empty, the direct socket address is always used — safe
    default that prevents header spoofing. Set TRUSTED_PROXIES to the IP of your
    Next.js / ingress container so per-IP lockout works correctly behind the proxy.
    """
    peer = request.client.host if request.client else ""
    s = get_settings()
    trusted = {p.strip() for p in s.trusted_proxies.split(",") if p.strip()}
    if trusted and peer in trusted:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer or "unknown"


async def login(request: Request) -> JSONResponse:
    """POST /auth/login — authenticate with username+password.

    Returns access_token + httpOnly refresh cookie on success.
    """
    try:
        body = await request.json()
    except Exception:
        return _ERROR_ENVELOPE("VALIDATION_ERROR", "Invalid JSON body")

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return _ERROR_ENVELOPE("VALIDATION_ERROR", "username and password required")

    ip = _client_ip(request)

    # Lockout check
    if await _check_lockout(username, ip):
        log_lockout(username, ip)
        return _ERROR_ENVELOPE(
            "RATE_LIMITED",
            "Too many login attempts. Try again later.",
            status=429,
        )

    user = await verify_password(username, password)
    if not user:
        log_login(username, success=False, ip=ip, reason="bad credentials")
        return _ERROR_ENVELOPE("UNAUTHENTICATED", "Invalid username or password", status=401)

    log_login(user["user_id"], success=True, ip=ip)

    # Issue tokens
    access_token = issue_user_token(user["user_id"], user["role"])
    jti = str(uuid.uuid4())
    s = get_settings()
    refresh_ttl = s.auth_refresh_ttl_seconds
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=refresh_ttl)).isoformat()
    refresh_token = issue_refresh_token(jti, user["user_id"])
    await store_refresh_token(jti, user["user_id"], expires_at)

    response = JSONResponse(
        {
            "access_token": access_token,
            "expires_in": s.auth_access_ttl_seconds,
            "token_type": "Bearer",
            "user": {"id": user["user_id"], "username": user["username"], "role": user["role"]},
        }
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        max_age=refresh_ttl,
        httponly=True,
        samesite="lax",
        secure=s.env == "production",
        path="/auth/refresh",
    )
    return response


async def refresh(request: Request) -> JSONResponse:
    """POST /auth/refresh — rotate refresh token.

    EC4: concurrent refresh race — 30 s grace window on most-recently-rotated jti.
    Reuse-detection: revoked jti presented outside grace window revokes family.
    """
    refresh_cookie = request.cookies.get("refresh_token")
    if not refresh_cookie:
        return _ERROR_ENVELOPE("UNAUTHENTICATED", "No refresh token", status=401)

    try:
        payload = decode_refresh_token(refresh_cookie)
    except AuthError as e:
        return _ERROR_ENVELOPE("UNAUTHENTICATED", str(e), status=401)

    jti = payload["jti"]
    user_id = payload["sub"]

    # Check if this JTI was already revoked
    record = await get_refresh_token(jti)
    if record is None:
        return _ERROR_ENVELOPE("UNAUTHENTICATED", "Unknown refresh token", status=401)

    if record["revoked"]:
        # EC4: grace window check
        rotated = record.get("rotated_at")
        if rotated:
            rotated_dt = datetime.fromisoformat(rotated)
            age = (datetime.now(timezone.utc) - rotated_dt).total_seconds()
            if age <= 30:
                # Concurrent refresh race — allow it, just issue a new pair
                logger.info("Refresh race detected for jti=%s, granting grace", jti[:8])
            else:
                # Genuine reuse — revoke all tokens for this user
                log_refresh(user_id, jti, revoked=True)
                await revoke_user_refresh_tokens(user_id)
                response = _ERROR_ENVELOPE(
                    "UNAUTHENTICATED", "Token reuse detected — all sessions revoked", status=401
                )
                response.delete_cookie("refresh_token", path="/auth/refresh")
                return response
        else:
            log_refresh(user_id, jti, revoked=True)
            await revoke_user_refresh_tokens(user_id)
            response = _ERROR_ENVELOPE(
                "UNAUTHENTICATED", "Token reuse detected — all sessions revoked", status=401
            )
            response.delete_cookie("refresh_token", path="/auth/refresh")
            return response

    # Revoke current jti
    await revoke_refresh_token(jti)

    # Issue new pair
    new_jti = str(uuid.uuid4())
    s = get_settings()
    refresh_ttl = s.auth_refresh_ttl_seconds
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=refresh_ttl)).isoformat()
    new_refresh = issue_refresh_token(new_jti, user_id)
    await store_refresh_token(new_jti, user_id, expires_at)

    # Mark old as rotated with timestamp for grace window
    await update_rotated_at(jti)

    access_token = issue_user_token(user_id)
    log_refresh(user_id, new_jti)

    response = JSONResponse(
        {
            "access_token": access_token,
            "expires_in": s.auth_access_ttl_seconds,
            "token_type": "Bearer",
        }
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        max_age=refresh_ttl,
        httponly=True,
        samesite="lax",
        secure=s.env == "production",
        path="/auth/refresh",
    )
    return response


async def logout(request: Request) -> JSONResponse:
    """POST /auth/logout — revoke refresh token and clear cookie."""
    refresh_cookie = request.cookies.get("refresh_token")
    if refresh_cookie:
        try:
            payload = decode_refresh_token(refresh_cookie)
            await revoke_refresh_token(payload["jti"])
        except AuthError:
            pass

    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("refresh_token", path="/auth/refresh")
    return response


async def me(request: Request) -> JSONResponse:
    """GET /auth/me — return the current user's principal info."""
    try:
        p = require(request, kinds=("user",))
    except AuthError as e:
        return _ERROR_ENVELOPE(str(e), "Not authenticated", status=401)

    from shared.memory.user_store import get_user

    user = await get_user(p.subject)
    if not user:
        return _ERROR_ENVELOPE("NOT_FOUND", "User not found", status=404)

    return JSONResponse(
        {
            "id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "disabled": user["disabled"],
        }
    )


def get_auth_routes() -> list[Route]:
    """Return auth route definitions for the orchestrator app."""
    return [
        Route("/auth/login", login, methods=["POST"]),
        Route("/auth/refresh", refresh, methods=["POST"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/auth/me", me, methods=["GET"]),
    ]
