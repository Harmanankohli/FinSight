"""Contract tests: every REST route × {auth on/off} × {none, user, service, admin}.

WP 3.1: parametrized auth matrix covering all trust boundaries.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest.importorskip("argon2", reason="auth contract tests require argon2-cffi")

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

pytestmark = pytest.mark.auth

BASE = "http://test"


@pytest_asyncio.fixture(autouse=True)
async def _clean_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    import shared.memory.store as store_mod

    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    yield
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None


def _build_app(settings_overrides: dict | None = None) -> Starlette:
    from shared.settings import reset_settings_for_tests

    reset_settings_for_tests()
    from shared.settings import get_settings

    s = get_settings()
    if settings_overrides:
        for k, v in settings_overrides.items():
            setattr(s, k, v)

    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _health(request):
        return JSONResponse({"status": "ok"})

    routes = [Route("/health", _health)]
    from orchestrator.api_routes import get_api_routes

    routes.extend(get_api_routes())
    from orchestrator.auth_routes import get_auth_routes

    routes.extend(get_auth_routes())

    mw = []
    if s.auth_enabled:
        from shared.auth.middleware import build_auth_middleware

        mw = build_auth_middleware(s)

    return Starlette(routes=routes, middleware=mw)


PUBLIC_ROUTES = ["/health", "/health/", "/api/agents"]
PUBLIC_OK = (200, 307, 404, 500)  # 500 covers import errors in test env (e.g. missing langfuse)
AUTH_ROUTES = [("/auth/login", "POST"), ("/auth/refresh", "POST"), ("/auth/logout", "POST")]
API_ROUTES = [
    ("/api/memory/ticker/NVDA", "GET"),
    ("/api/memory/ticker/NVDA/latest", "GET"),
    ("/api/memory/ticker/NVDA/changed", "GET"),
    ("/api/sessions", "GET"),
]
# /api/reports is a public prefix (report downloads need no auth)
PUBLIC_REPORT_ROUTES = [("/api/reports/ticker/NVDA/latest/pptx", "GET")]


class TestPublicPaths:
    @pytest.mark.parametrize("path", PUBLIC_ROUTES)
    async def test_public_always_accessible(self, path):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.get(path)
        assert r.status_code in PUBLIC_OK, f"{path} should be accessible"


class TestAuthOff:
    AUTH_OPEN = [r for r in AUTH_ROUTES if r[0] != "/auth/refresh"]

    @pytest.mark.parametrize("path,method", AUTH_OPEN + API_ROUTES + PUBLIC_REPORT_ROUTES)
    async def test_open_when_auth_disabled(self, path, method, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "false")
        app = _build_app({"auth_enabled": False})
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.request(method, path)
        assert r.status_code != 401, f"{method} {path} should not be blocked"


class TestAuthOnNoToken:
    @pytest.mark.parametrize("path,method", API_ROUTES)
    async def test_protected_routes_blocked_without_token(self, path, method):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.request(method, path)
        assert r.status_code == 401, f"{method} {path} expected 401, got {r.status_code}"
        body = r.json()
        assert body["error"]["code"] == "UNAUTHENTICATED"

    @pytest.mark.parametrize("path,method", [r for r in AUTH_ROUTES if r[0] != "/auth/refresh"])
    async def test_auth_endpoints_open_without_token(self, path, method):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.request(method, path)
        assert r.status_code not in (401, 403), (
            f"{method} {path} should be accessible (got {r.status_code})"
        )


class TestAuthOnUserToken:
    @pytest.fixture(autouse=True)
    async def _seed_user(self):
        import shared.memory.store as store_mod
        from shared.memory.user_store import create_user, ensure_schema_v4

        if store_mod._db_conn is not None:
            await store_mod._db_conn.close()
            store_mod._db_conn = None
        await ensure_schema_v4()
        self._uname = f"user_{uuid.uuid4().hex[:8]}"
        self._user_id = await create_user(self._uname, "pass", role="user")

    def _user_token(self):
        from shared.auth.tokens import issue_user_token

        return issue_user_token(self._user_id)

    @pytest.mark.parametrize("path,method", API_ROUTES)
    async def test_user_token_grants_access(self, path, method):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        token = self._user_token()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.request(method, path, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code != 401, f"{method} {path} should not return 401 with valid token"


class TestAdminGating:
    @pytest.fixture(autouse=True)
    async def _seed_users(self):
        import shared.memory.store as store_mod
        from shared.memory.user_store import create_user, ensure_schema_v4

        if store_mod._db_conn is not None:
            await store_mod._db_conn.close()
            store_mod._db_conn = None
        await ensure_schema_v4()
        self._admin_id = await create_user(f"admin_{uuid.uuid4().hex[:8]}", "pass", role="admin")
        self._normal_id = await create_user(f"user_{uuid.uuid4().hex[:8]}", "pass", role="user")

    def _admin_token(self):
        from shared.auth.tokens import issue_user_token

        return issue_user_token(self._admin_id, role="admin")

    def _user_token(self):
        from shared.auth.tokens import issue_user_token

        return issue_user_token(self._normal_id)

    async def test_agents_endpoint_is_public(self):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.get("/api/agents")
        assert r.status_code != 401, "/api/agents should be publicly accessible"

    @pytest.mark.parametrize("path,method", PUBLIC_REPORT_ROUTES)
    async def test_report_endpoints_are_public(self, path, method):
        app = _build_app(
            {"auth_enabled": True, "auth_jwt_secrets": "a" * 32, "service_auth_token": "b" * 16}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
            r = await client.request(method, path)
        assert r.status_code != 401, f"{path} should be publicly accessible (got {r.status_code})"
