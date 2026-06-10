"""Authentication & authorization toolkit — tokens, middleware, audit.

WP 2.1: shared auth primitives. WP 2.2 extends with user store + login routes.
"""

from shared.auth.tokens import AuthError, Principal, issue_user_token, verify_user_token
from shared.auth.middleware import (
    PUBLIC_PATHS,
    AuthMiddleware,
    build_auth_middleware,
    get_principal,
    require,
)

__all__ = [
    "AuthError",
    "Principal",
    "issue_user_token",
    "verify_user_token",
    "AuthMiddleware",
    "build_auth_middleware",
    "get_principal",
    "require",
    "PUBLIC_PATHS",
]
