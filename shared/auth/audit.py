"""Audit-log helpers for auth events.

Logs login success/failure, lockout, token refresh, and admin actions
with structured fields for log aggregation.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("auth")


def log_login(user_id: str, success: bool, ip: str = "", reason: str = "") -> None:
    extra = {"user_id": user_id, "ip": ip, "success": success}
    if reason:
        extra["reason"] = reason
    if success:
        logger.info("Login success user=%s ip=%s", user_id, ip, extra=extra)
    else:
        logger.warning("Login failure user=%s ip=%s reason=%s", user_id, ip, reason, extra=extra)


def log_lockout(user_id: str, ip: str = "") -> None:
    extra = {"user_id": user_id, "ip": ip}
    logger.warning("Account locked user=%s ip=%s", user_id, ip, extra=extra)


def log_refresh(user_id: str, jti: str, revoked: bool = False) -> None:
    extra = {"user_id": user_id, "jti": jti[:8], "revoked": revoked}
    if revoked:
        logger.warning("Refresh reuse detected user=%s jti=%s", user_id, jti[:8], extra=extra)
    else:
        logger.info("Refresh token rotated user=%s jti=%s", user_id, jti[:8], extra=extra)


def log_admin_action(admin_id: str, action: str, details: str = "") -> None:
    extra = {"admin_id": admin_id, "action": action}
    logger.info(
        "Admin action admin=%s action=%s details=%s", admin_id, action, details, extra=extra
    )


def log_sandbox_execution(
    principal: str, code_sha256: str, duration: float, exit_status: int
) -> None:
    extra = {
        "principal": principal,
        "code_sha256": code_sha256,
        "duration_ms": int(duration * 1000),
        "exit_status": exit_status,
    }
    logger.info(
        "Sandbox execution principal=%s sha256=%s", principal, code_sha256[:12], extra=extra
    )
