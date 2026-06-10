import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure test-safe env vars so no real LLM/Langfuse calls leak out."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    from shared.settings import reset_settings_for_tests
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


@pytest_asyncio.fixture
async def memory_db(tmp_path):
    """Provide a fresh SQLite db path for memory layer tests.

    Resets the module-level singleton connection so each test gets an
    isolated database — no cross-test state leaks.
    """
    import shared.memory.store as store_mod

    old_conn = store_mod._db_conn
    store_mod._db_conn = None
    db_path = tmp_path / "test_finsight.db"
    yield db_path
    if store_mod._db_conn is not None:
        await store_mod._db_conn.close()
        store_mod._db_conn = None
    store_mod._db_conn = old_conn
