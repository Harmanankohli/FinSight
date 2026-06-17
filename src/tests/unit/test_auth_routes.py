"""Tests for orchestrator/auth_routes.py — login, refresh, logout, me endpoints."""

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware import Middleware

from shared.settings import reset_settings_for_tests


@pytest_asyncio.fixture(autouse=True)
async def _clean_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    reset_settings_for_tests()
    import shared.memory.store as store_mod

    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    db_path = store_mod.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                pass
    import shared.memory.user_store as us_mod
    us_mod._schema_v4_ensured = False
    yield
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    reset_settings_for_tests()


@pytest_asyncio.fixture
async def app_with_user():
    """Build a minimal Starlette app with auth routes + middleware + a test user."""
    from orchestrator.auth_routes import get_auth_routes
    from shared.auth.middleware import AuthMiddleware
    from shared.memory.user_store import create_user, ensure_schema_v4

    await ensure_schema_v4()
    username = f"testuser_{uuid.uuid4().hex[:8]}"
    await create_user(username, "testpass", role="user")

    _app = Starlette(
        routes=get_auth_routes(),
        middleware=[Middleware(AuthMiddleware)],
    )
    _app.state._test_username = username
    return _app


def _uname(app) -> str:
    return getattr(app.state, "_test_username", "testuser")


@pytest.mark.asyncio
async def test_login_success(app_with_user):
    uname = _uname(app_with_user)
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/login", json={"username": uname, "password": "testpass"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert data["user"]["username"] == uname
    assert "refresh_token" in resp.cookies


@pytest.mark.asyncio
async def test_login_bad_password(app_with_user):
    uname = _uname(app_with_user)
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/login", json={"username": uname, "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_login_nonexistent_user(app_with_user):
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_missing_fields(app_with_user):
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/login", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_refresh_token_flow(app_with_user):
    uname = _uname(app_with_user)
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_resp = await client.post(
            "/auth/login", json={"username": uname, "password": "testpass"}
        )
        assert login_resp.status_code == 200

        refresh_resp = await client.post("/auth/refresh")
        assert refresh_resp.status_code == 200
        data = refresh_resp.json()
        assert "access_token" in data


@pytest.mark.asyncio
async def test_logout(app_with_user):
    uname = _uname(app_with_user)
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/auth/login", json={"username": uname, "password": "testpass"})
        resp = await client.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged_out"


@pytest.mark.asyncio
async def test_me_authenticated(app_with_user):
    uname = _uname(app_with_user)
    from shared.memory.user_store import get_user_by_username

    user = await get_user_by_username(uname)
    from shared.auth.tokens import issue_user_token

    token = issue_user_token(user["user_id"])

    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == uname
    assert data["role"] == "user"


@pytest.mark.asyncio
async def test_me_unauthenticated(app_with_user):
    transport = ASGITransport(app=app_with_user)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/me")
    assert resp.status_code == 401
