"""Tests for shared/memory/user_store.py — user CRUD, password hashing, refresh token lifecycle."""

import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from shared.settings import reset_settings_for_tests


@pytest_asyncio.fixture(autouse=True)
async def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    reset_settings_for_tests()
    import shared.memory.store as store_mod

    # Close any existing DB connection and redirect to a per-test temp DB
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    db_path = tmp_path / "test_users.db"
    store_mod.DB_PATH = db_path
    # Pre-open connection to temp path — get_db()'s default arg captures the
    # original DB_PATH at function definition time, so we must pass explicitly.
    from shared.memory.store import get_db
    await get_db(db_path)
    import shared.memory.user_store as us_mod
    us_mod._schema_v4_ensured = False
    yield
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    reset_settings_for_tests()


@pytest.mark.asyncio
async def test_create_user():
    from shared.memory.user_store import create_user, get_user_by_username

    user_id = await create_user("alice", "secret123", role="user")
    assert user_id is not None
    user = await get_user_by_username("alice")
    assert user is not None
    assert user["username"] == "alice"
    assert user["role"] == "user"
    assert not user["disabled"]


@pytest.mark.asyncio
async def test_create_admin():
    from shared.memory.user_store import create_user, get_user

    user_id = await create_user("admin", "adminpass", role="admin")
    user = await get_user(user_id)
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_duplicate_username_raises():
    from shared.memory.user_store import create_user

    await create_user("bob", "pass1")
    with pytest.raises(ValueError, match="already exist"):
        await create_user("bob", "pass2")


@pytest.mark.asyncio
async def test_verify_password_success():
    from shared.memory.user_store import create_user, verify_password

    await create_user("carol", "mypassword")
    user = await verify_password("carol", "mypassword")
    assert user is not None
    assert user["username"] == "carol"


@pytest.mark.asyncio
async def test_verify_password_failure():
    from shared.memory.user_store import create_user, verify_password

    await create_user("dave", "correct")
    user = await verify_password("dave", "wrong")
    assert user is None


@pytest.mark.asyncio
async def test_verify_password_nonexistent_user():
    from shared.memory.user_store import verify_password

    user = await verify_password("nobody", "anything")
    assert user is None


@pytest.mark.asyncio
async def test_refresh_token_lifecycle():
    from shared.memory.user_store import (
        create_user,
        get_refresh_token,
        is_refresh_token_revoked,
        revoke_refresh_token,
        store_refresh_token,
    )

    user_id = await create_user("eve", "pass")
    jti = str(uuid.uuid4())
    await store_refresh_token(jti, user_id, "2099-01-01T00:00:00")

    token = await get_refresh_token(jti)
    assert token is not None
    assert not token["revoked"]
    assert not await is_refresh_token_revoked(jti)

    await revoke_refresh_token(jti)
    assert await is_refresh_token_revoked(jti)
    token = await get_refresh_token(jti)
    assert token["revoked"]


@pytest.mark.asyncio
async def test_unknown_refresh_is_revoked():
    from shared.memory.user_store import is_refresh_token_revoked

    assert await is_refresh_token_revoked("nonexistent-jti")


@pytest.mark.asyncio
async def test_revoke_user_all_tokens():
    from shared.memory.user_store import (
        create_user,
        is_refresh_token_revoked,
        revoke_user_refresh_tokens,
        store_refresh_token,
    )

    user_id = await create_user("frank", "pass")
    jti1, jti2 = str(uuid.uuid4()), str(uuid.uuid4())
    await store_refresh_token(jti1, user_id, "2099-01-01T00:00:00")
    await store_refresh_token(jti2, user_id, "2099-01-01T00:00:00")

    count = await revoke_user_refresh_tokens(user_id)
    assert count == 2

    assert await is_refresh_token_revoked(jti1)
    assert await is_refresh_token_revoked(jti2)


@pytest.mark.asyncio
async def test_rotated_at():
    from shared.memory.user_store import (
        create_user,
        get_refresh_token,
        store_refresh_token,
        update_rotated_at,
    )

    user_id = await create_user("grace", "pass")
    jti = str(uuid.uuid4())
    await store_refresh_token(jti, user_id, "2099-01-01T00:00:00")
    await update_rotated_at(jti)
    token = await get_refresh_token(jti)
    assert token["rotated_at"] is not None
