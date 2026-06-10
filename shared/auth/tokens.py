"""JWT token creation and verification for user and service authentication.

Issuer: finsight. Token types: access, refresh, service.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from shared.settings import get_settings

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None  # type: ignore[assignment]


class AuthError(Exception):
    """Raised when token verification fails."""


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "service"]
    subject: str
    role: str = "user"


def issue_user_token(
    user_id: str, role: str = "user", *, ttl: int | None = None
) -> str:
    """Issue a signed access JWT for a user.

    Uses the first key from AUTH_JWT_SECRETS as the signing key.
    """
    if pyjwt is None:
        raise AuthError("pyjwt library not installed")
    s = get_settings()
    key = s.auth_jwt_secrets.split(",")[0]
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": user_id,
            "role": role,
            "iat": now,
            "exp": now + (ttl or s.auth_access_ttl_seconds),
            "iss": "finsight",
            "type": "access",
        },
        key,
        algorithm="HS256",
    )


def issue_refresh_token(
    jti: str, user_id: str, *, ttl: int | None = None
) -> str:
    """Issue a refresh JWT with a unique JWT ID for rotation tracking."""
    if pyjwt is None:
        raise AuthError("pyjwt library not installed")
    s = get_settings()
    key = s.auth_jwt_secrets.split(",")[0]
    now = int(time.time())
    return pyjwt.encode(
        {
            "jti": jti,
            "sub": user_id,
            "iat": now,
            "exp": now + (ttl or s.auth_refresh_ttl_seconds),
            "iss": "finsight",
            "type": "refresh",
        },
        key,
        algorithm="HS256",
    )


def verify_user_token(token: str) -> Principal:
    """Verify a JWT against all configured secrets (key rotation).

    Tries each comma-separated secret. Returns the decoded Principal
    on first success. Raises AuthError if no key validates or the
    token type is wrong.
    """
    if pyjwt is None:
        raise AuthError("pyjwt library not installed")
    last: Exception | None = None
    for key in get_settings().auth_jwt_secrets.split(","):
        if not key:
            continue
        try:
            c = pyjwt.decode(token, key, algorithms=["HS256"], issuer="finsight")
            if c.get("type") not in ("access",):
                raise AuthError("wrong token type")
            return Principal("user", c["sub"], c.get("role", "user"))
        except pyjwt.PyJWTError as e:
            last = e
    raise AuthError("invalid token") from last


def decode_refresh_token(token: str) -> dict:
    """Decode a refresh token, returning the payload dict.

    Raises AuthError on failure.
    """
    if pyjwt is None:
        raise AuthError("pyjwt library not installed")
    last: Exception | None = None
    for key in get_settings().auth_jwt_secrets.split(","):
        if not key:
            continue
        try:
            c = pyjwt.decode(token, key, algorithms=["HS256"], issuer="finsight")
            if c.get("type") != "refresh":
                raise AuthError("not a refresh token")
            return c
        except pyjwt.PyJWTError as e:
            last = e
    raise AuthError("invalid refresh token") from last
