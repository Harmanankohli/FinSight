"""Two-level MCP cache: in-process L1 (TTLCache) + Redis L2 for cross-worker sharing.

Set REDIS_URL in .env (e.g. redis://localhost:6379/0) to activate Redis.
Falls back silently to in-process TTLCache when REDIS_URL is absent or Redis
is unreachable — no restart required.

Usage:
    from shared.redis_cache import make_cache
    _cache_prices = make_cache(ttl_seconds=60, namespace="prices")

Interface is identical to TTLCache: get_or_fetch(), get(), set().
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from shared.logging_config import logged, logged_sync
from shared.ttl_cache import TTLCache

logger = logging.getLogger(__name__)


class RedisCache:
    """Two-level cache: TTLCache L1 (fast, per-process) + Redis L2 (shared across workers).

    Read path: L1 hit → return; L2 hit → populate L1, return; miss → fetch, write-through.
    Write path: update L1 synchronously, fire-and-forget async write to Redis.
    Redis errors are silently swallowed — the cache degrades to L1-only.
    """

    def __init__(
        self,
        ttl_seconds: float | None,
        namespace: str,
        max_entries: int = 512,
    ) -> None:
        self._ttl_int = int(ttl_seconds) if (ttl_seconds is not None and ttl_seconds != float("inf")) else None
        self._ns = namespace
        self._l1 = TTLCache(ttl_seconds=ttl_seconds, max_entries=max_entries)
        self._redis: Any = None
        self._redis_lock = asyncio.Lock()
        self._redis_failed = False

    def _key(self, k: str) -> str:
        return f"finsight:{self._ns}:{k}"

    async def _get_redis(self) -> Any:
        if self._redis_failed:
            return None
        if self._redis is not None:
            return self._redis
        async with self._redis_lock:
            if self._redis is not None:
                return self._redis
            try:
                import redis.asyncio as aioredis  # optional dep
                from shared.settings import REDIS_URL
                client = aioredis.from_url(
                    REDIS_URL,
                    decode_responses=False,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await client.ping()
                self._redis = client
                logger.info("Redis cache connected (ns=%s)", self._ns)
            except Exception as exc:
                logger.warning(
                    "Redis unavailable for ns=%s, falling back to in-process cache: %s",
                    self._ns, exc,
                )
                self._redis_failed = True
                return None
        return self._redis

    async def _redis_get(self, key: str) -> Any:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            raw = await client.get(self._key(key))
            return json.loads(raw) if raw is not None else None
        except Exception:
            return None

    async def _redis_set(self, key: str, value: Any) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            serialized = json.dumps(value)
            if self._ttl_int:
                await client.setex(self._key(key), self._ttl_int, serialized)
            else:
                await client.set(self._key(key), serialized)
        except Exception as exc:
            logger.debug("Redis set failed for %s: %s", key, exc)

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return cached value (L1 then L2) or fetch and populate both levels."""
        l1_val = self._l1.get(key)
        if l1_val is not None:
            return l1_val
        l2_val = await self._redis_get(key)
        if l2_val is not None:
            self._l1.set(key, l2_val)
            return l2_val
        value = await self._l1.get_or_fetch(key, fetch)
        asyncio.create_task(self._redis_set(key, value))
        return value

    def get(self, key: str) -> Any:
        """Synchronous L1 read — used by callers that manage their own fetch logic."""
        return self._l1.get(key)

    def set(self, key: str, val: Any) -> None:
        """Write to L1 and fire-and-forget write to Redis."""
        self._l1.set(key, val)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._redis_set(key, val))
        except RuntimeError:
            pass  # no running event loop (e.g. module import time)


def make_cache(
    ttl_seconds: float | None,
    namespace: str,
    max_entries: int = 512,
) -> RedisCache | TTLCache:
    """Return RedisCache if REDIS_URL is configured, else TTLCache.

    Callers need no conditional logic — the returned object has an identical
    interface regardless of which implementation is returned.
    """
    from shared.settings import REDIS_URL
    if REDIS_URL:
        return RedisCache(ttl_seconds=ttl_seconds, namespace=namespace, max_entries=max_entries)
    return TTLCache(ttl_seconds=ttl_seconds, max_entries=max_entries)
