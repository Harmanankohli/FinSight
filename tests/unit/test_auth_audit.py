"""Tests for shared/auth/audit.py — structured audit logging."""
import logging
from shared.auth.audit import log_login, log_lockout, log_refresh, log_admin_action, log_sandbox_execution


class TestAuditLog:
    def test_log_login_success(self, caplog):
        caplog.set_level(logging.INFO)
        log_login("user-1", True, ip="127.0.0.1")
        assert any("Login success" in r.message for r in caplog.records)

    def test_log_login_failure(self, caplog):
        caplog.set_level(logging.WARNING)
        log_login("user-1", False, reason="bad password")
        assert any("Login failure" in r.message for r in caplog.records)
        assert any("bad password" in r.message for r in caplog.records)

    def test_log_lockout(self, caplog):
        caplog.set_level(logging.WARNING)
        log_lockout("user-1", ip="10.0.0.1")
        assert any("Account locked" in r.message for r in caplog.records)

    def test_log_refresh(self, caplog):
        caplog.set_level(logging.INFO)
        log_refresh("user-1", "jti-abcd1234")
        assert any("token rotated" in r.message for r in caplog.records)

    def test_log_refresh_revoked(self, caplog):
        caplog.set_level(logging.WARNING)
        log_refresh("user-1", "jti-bad", revoked=True)
        assert any("reuse detected" in r.message for r in caplog.records)

    def test_log_admin_action(self, caplog):
        caplog.set_level(logging.INFO)
        log_admin_action("admin-1", "delete_user", details="user-2")
        assert any("Admin action" in r.message for r in caplog.records)

    def test_log_sandbox_execution(self, caplog):
        caplog.set_level(logging.INFO)
        log_sandbox_execution("user-1", "abc123", 0.5, 0)
        assert any("Sandbox execution" in r.message for r in caplog.records)
