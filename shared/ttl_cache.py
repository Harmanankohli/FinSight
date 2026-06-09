"""Async TTL cache with single-flight deduplication.

Prevents duplicate concurrent fetches for the same key — ten simultaneous
calls for the same ticker make exactly one network request.
"""
import asyncio
import time
from typing import Any, Callable, Awaitable

from shared.logging_config import logged, logged_sync


class TTLCache:
    """TTL cache with asyncio single-flight deduplication.

    Concurrent callers with the same key share a single in-flight fetch
    instead of each triggering their own network call.
    """

    def __init__(self, ttl_seconds: float | None, max_entries: int = 512):
        self._ttl = ttl_seconds if ttl_seconds is not None else float("inf")
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future] = {}

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return cached value if fresh; fetch exactly once for concurrent callers."""
        entry = self._data.get(key)
        if entry and time.monotonic() - entry[0] < self._ttl:
            return entry[1]

        async with self._lock:
            entry = self._data.get(key)
            if entry and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
            if key in self._inflight:
                fut = self._inflight[key]
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._inflight[key] = fut
                asyncio.create_task(self._do_fetch(key, fetch, fut))
        return await fut

    async def _do_fetch(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Any]],
        fut: asyncio.Future,
    ) -> None:
        try:
            value = await fetch()
            self._data[key] = (time.monotonic(), value)
            if len(self._data) > self._max:
                self._data.pop(next(iter(self._data)))
            fut.set_result(value)
        except Exception as exc:
            fut.set_exception(exc)
        finally:
            self._inflight.pop(key, None)

    def get(self, key: str) -> Any:
        """Synchronous read — returns cached value if fresh, None on miss/expiry."""
        entry = self._data.get(key)
        if entry and time.monotonic() - entry[0] < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, val: Any) -> None:
        """Direct cache write, evicting oldest entry if at capacity."""
        self._data[key] = (time.monotonic(), val)
        if len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))
