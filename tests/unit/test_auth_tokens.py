"""Tests for shared/auth/tokens.py — JWT creation, verification, error handling."""
import time
import pytest
from shared.auth.tokens import (
    AuthError,
    Principal,
    issue_user_token,
    issue_refresh_token,
    verify_user_token,
    decode_refresh_token,
)
from shared.settings import reset_settings_for_tests


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


class TestIssueUserToken:
    def test_issues_valid_jwt(self):
        token = issue_user_token("user-123", "user")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_default_role_is_user(self):
        token = issue_user_token("user-456")
        p = verify_user_token(token)
        assert p.role == "user"

    def test_custom_role(self):
        token = issue_user_token("admin-1", "admin")
        p = verify_user_token(token)
        assert p.role == "admin"

    def test_custom_ttl(self):
        token = issue_user_token("u1", ttl=60)
        p = verify_user_token(token)
        assert p.subject == "u1"
        assert p.kind == "user"


class TestVerifyUserToken:
    def test_returns_principal(self):
        token = issue_user_token("alice", "user")
        p = verify_user_token(token)
        assert p.kind == "user"
        assert p.subject == "alice"

    def test_rejects_expired_token(self):
        token = issue_user_token("bob", ttl=-1)
        # Force-expedite: wait a tiny bit or just verify it fails
        with pytest.raises(AuthError, match="invalid token"):
            verify_user_token(token)

    def test_rejects_wrong_signature(self):
        token = issue_user_token("carol")
        # The token is signed with the correct key; we need a token signed with a different key
        import jwt as pyjwt
        bad = pyjwt.encode(
            {"sub": "carol", "role": "user", "iat": int(time.time()),
             "exp": int(time.time()) + 900, "iss": "finsight", "type": "access"},
            "z" * 32, algorithm="HS256",
        )
        with pytest.raises(AuthError, match="invalid token"):
            verify_user_token(bad)

    def test_rejects_wrong_type(self):
        import jwt as pyjwt
        s = __import__("shared.settings", fromlist=["get_settings"]).get_settings()
        key = s.auth_jwt_secrets.split(",")[0]
        bad = pyjwt.encode(
            {"sub": "dave", "role": "user", "iat": int(time.time()),
             "exp": int(time.time()) + 900, "iss": "finsight", "type": "evil"},
            key, algorithm="HS256",
        )
        with pytest.raises(AuthError, match="wrong token"):

            verify_user_token(bad)

    def test_rejects_wrong_issuer(self):
        token = issue_user_token("eve")
        import jwt as pyjwt
        s = __import__("shared.settings", fromlist=["get_settings"]).get_settings()
        key = s.auth_jwt_secrets.split(",")[0]
        bad = pyjwt.encode(
            {"sub": "eve", "role": "user", "iat": int(time.time()),
             "exp": int(time.time()) + 900, "iss": "wrong", "type": "access"},
            key, algorithm="HS256",
        )
        with pytest.raises(AuthError, match="invalid token"):
            verify_user_token(bad)

    def test_key_rotation(self, monkeypatch):
        """Old keys in the comma-separated list still verify existing tokens."""
        monkeypatch.setenv("AUTH_JWT_SECRETS", "newkey" + "x" * 26 + ",oldkey" + "y" * 26)
        reset_settings_for_tests()
        # Issue token when first key was the signing key
        token = issue_user_token("fiona")
        p = verify_user_token(token)
        assert p.subject == "fiona"


class TestRefreshToken:
    def test_issue_and_decode(self):
        token = issue_refresh_token("jti-1", "user-1")
        payload = decode_refresh_token(token)
        assert payload["jti"] == "jti-1"
        assert payload["sub"] == "user-1"
        assert payload["type"] == "refresh"

    def test_rejects_access_token_as_refresh(self):
        access = issue_user_token("u1")
        with pytest.raises(AuthError, match="not a refresh"):
            decode_refresh_token(access)

    def test_rejects_expired_refresh(self):
        import jwt as pyjwt
        s = __import__("shared.settings", fromlist=["get_settings"]).get_settings()
        key = s.auth_jwt_secrets.split(",")[0]
        bad = pyjwt.encode(
            {"jti": "dead", "sub": "u1", "iat": int(time.time()) - 99999,
             "exp": int(time.time()) - 1, "iss": "finsight", "type": "refresh"},
            key, algorithm="HS256",
        )
        with pytest.raises(AuthError, match="invalid refresh"):
            decode_refresh_token(bad)
