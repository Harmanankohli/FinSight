"""Tests for shared/auth/middleware.py — AuthMiddleware, get_principal, require."""
import json
import pytest
from shared.auth.middleware import (
    PUBLIC_PATHS,
    AuthMiddleware,
    build_auth_middleware,
    get_principal,
    require,
)
from shared.auth.tokens import AuthError, Principal, issue_user_token
from shared.settings import Settings, reset_settings_for_tests


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


def _make_scope(method="GET", path="/api/test", headers=None):
    h = [(b"host", b"localhost")]
    if headers:
        h.extend(headers)
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": h,
        "query_string": b"",
    }


async def _ok_app(scope, receive, send):
    body = b'{"ok":true}'
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def _run_middleware(mw, scope):
    """Run middleware with _ok_app and capture response."""
    responses = []

    async def send(event):
        responses.append(event)

    await mw(scope, None, send)
    return responses


class TestPublicPaths:
    @pytest.mark.parametrize("path", ["/health", "/health/", "/.well-known/anything", "/.well-known/a2a"])
    async def test_public_paths_pass(self, path):
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(path=path)
        responses = await _run_middleware(mw, scope)
        # Should get the 200 from _ok_app
        assert any(r.get("status") == 200 for r in responses if r["type"] == "http.response.start")

    @pytest.mark.parametrize("path", ["/api/test", "/a2a", "/reports/file.pptx"])
    async def test_non_public_paths_blocked_without_auth(self, path):
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(path=path)
        responses = await _run_middleware(mw, scope)

        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 401
        body = next(r for r in responses if r["type"] == "http.response.body")
        payload = json.loads(body["body"])
        assert payload["error"]["code"] == "UNAUTHENTICATED"


class TestBearerToken:
    async def test_valid_service_token(self):
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(headers=[(b"authorization", b"Bearer " + b"b" * 16)])
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 200
        assert scope.get("finsight.principal") is not None
        assert scope["finsight.principal"].kind == "service"

    async def test_valid_user_jwt(self):
        token = issue_user_token("alice")
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(headers=[(b"authorization", f"Bearer {token}".encode())])
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 200
        assert scope["finsight.principal"].kind == "user"
        assert scope["finsight.principal"].subject == "alice"

    async def test_missing_header(self):
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope()
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 401

    async def test_empty_bearer_rejected(self):
        """E4: empty/short credentials NEVER match."""
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(headers=[(b"authorization", b"Bearer ")])
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 401

    async def test_short_bearer_rejected(self):
        """E4: token shorter than 16 chars rejected."""
        mw = AuthMiddleware(_ok_app)
        scope = _make_scope(headers=[(b"authorization", b"Bearer short")])
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 401

    async def test_user_token_on_service_only_route_returns_403(self):
        token = issue_user_token("bob")
        mw = AuthMiddleware(_ok_app, accept=frozenset({"service"}))
        scope = _make_scope(headers=[(b"authorization", f"Bearer {token}".encode())])
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 403
        body = next(r for r in responses if r["type"] == "http.response.body")
        payload = json.loads(body["body"])
        assert payload["error"]["code"] == "FORBIDDEN"


class TestLifecycleScope:
    async def test_lifespan_passes_through(self):
        """EC3: non-http scopes pass through untouched."""
        mw = AuthMiddleware(_ok_app)
        scope = {"type": "lifespan"}
        responses = await _run_middleware(mw, scope)
        start = next(r for r in responses if r["type"] == "http.response.start")
        assert start["status"] == 200  # _ok_app sends 200


class TestBuildAuthMiddleware:
    def test_disabled_returns_empty(self):
        settings = Settings(auth_enabled=False)
        mw_list = build_auth_middleware(settings)
        assert mw_list == []

    def test_enabled_returns_auth_middleware(self):
        settings = Settings(
            auth_enabled=True,
            auth_jwt_secrets="a" * 32,
            service_auth_token="b" * 16,
        )
        mw_list = build_auth_middleware(settings)
        assert len(mw_list) == 1
        from starlette.middleware import Middleware
        assert isinstance(mw_list[0], Middleware)
        assert mw_list[0].cls is AuthMiddleware


class TestHelpers:
    def test_get_principal_none_when_not_set(self):
        p = get_principal({"type": "http"})
        assert p is None

    def test_get_principal_returns_principal(self):
        scope = {"finsight.principal": Principal("user", "alice")}
        p = get_principal(scope)
        assert p is not None
        assert p.subject == "alice"

    def test_require_raises_when_none(self):
        with pytest.raises(AuthError, match="UNAUTHENTICATED"):
            require({"type": "http"})

    def test_require_wrong_kind(self):
        scope = {"finsight.principal": Principal("service", "mcp")}
        with pytest.raises(AuthError, match="FORBIDDEN"):
            require(scope, kinds=("user",))

    def test_require_role_mismatch(self):
        scope = {"finsight.principal": Principal("user", "bob", role="user")}
        with pytest.raises(AuthError, match="FORBIDDEN"):
            require(scope, kinds=("user",), role="admin")

    def test_require_passes(self):
        scope = {"finsight.principal": Principal("user", "alice", role="admin")}
        p = require(scope, kinds=("user",), role="admin")
        assert p.subject == "alice"
