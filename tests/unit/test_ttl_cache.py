import asyncio
import pytest
from shared.ttl_cache import TTLCache


async def test_cache_miss_calls_fetch():
    cache = TTLCache(ttl_seconds=60)
    calls = []

    async def fetch():
        calls.append(1)
        return "result"

    val = await cache.get_or_fetch("k", fetch)
    assert val == "result"
    assert len(calls) == 1


async def test_cache_hit_does_not_refetch():
    cache = TTLCache(ttl_seconds=60)
    calls = []

    async def fetch():
        calls.append(1)
        return "cached"

    await cache.get_or_fetch("k", fetch)
    await cache.get_or_fetch("k", fetch)
    assert len(calls) == 1


async def test_cache_expiry_triggers_refetch():
    cache = TTLCache(ttl_seconds=0.05)
    calls = []

    async def fetch():
        calls.append(1)
        return "val"

    await cache.get_or_fetch("k", fetch)
    await asyncio.sleep(0.1)
    await cache.get_or_fetch("k", fetch)
    assert len(calls) == 2


async def test_single_flight_concurrent_callers():
    """Multiple concurrent callers for the same key share exactly one fetch."""
    cache = TTLCache(ttl_seconds=60)
    calls = []

    async def slow_fetch():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "data"

    results = await asyncio.gather(
        cache.get_or_fetch("k", slow_fetch),
        cache.get_or_fetch("k", slow_fetch),
        cache.get_or_fetch("k", slow_fetch),
    )
    assert all(r == "data" for r in results)
    assert len(calls) == 1


async def test_different_keys_each_fetch_independently():
    cache = TTLCache(ttl_seconds=60)
    calls: dict[str, int] = {}

    async def make_fetch(key: str):
        async def _fetch():
            calls[key] = calls.get(key, 0) + 1
            return f"val-{key}"
        return _fetch

    await cache.get_or_fetch("a", await make_fetch("a"))
    await cache.get_or_fetch("b", await make_fetch("b"))
    assert calls == {"a": 1, "b": 1}


def test_sync_get_returns_none_on_miss():
    cache = TTLCache(ttl_seconds=60)
    assert cache.get("missing") is None


def test_sync_set_and_get():
    cache = TTLCache(ttl_seconds=60)
    cache.set("k", 42)
    assert cache.get("k") == 42


def test_eviction_at_max_capacity():
    cache = TTLCache(ttl_seconds=60, max_entries=3)
    for i in range(4):
        cache.set(f"k{i}", i)
    assert len(cache._data) == 3
    # Oldest key (k0) evicted
    assert "k0" not in cache._data


async def test_fetch_exception_propagates():
    cache = TTLCache(ttl_seconds=60)

    async def bad_fetch():
        raise ValueError("fetch failed")

    with pytest.raises(ValueError, match="fetch failed"):
        await cache.get_or_fetch("k", bad_fetch)
