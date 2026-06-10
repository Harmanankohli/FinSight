"""Priority queue for LLM inference slots.

All agents share a single local LM Studio instance. When eval scoring runs
alongside production inference, eval calls can starve answer generation.
This module provides a process-local priority semaphore: production calls
(CRITICAL) are served before eval calls (LOW).

Usage:
    from shared.llm_queue import llm_queue, Priority

    async with llm_queue.acquire(Priority.CRITICAL, "quant-summary"):
        response = await llm.ainvoke(prompt)
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from contextlib import asynccontextmanager
from enum import IntEnum

from shared.settings import LLM_MAX_CONCURRENT

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    CRITICAL = 0  # answer generation — never starved
    NORMAL = 1  # warmup, misc
    LOW = 2  # eval scoring — yields to production


class LLMPriorityQueue:
    """Async semaphore that serves higher-priority callers first.

    When all ``max_concurrent`` slots are occupied, new callers queue.
    Slots are handed to the highest-priority (lowest enum value) waiter,
    with FIFO tie-breaking within the same priority level.
    """

    def __init__(self, max_concurrent: int = LLM_MAX_CONCURRENT):
        self._max = max_concurrent
        self._active = 0
        self._seq = 0
        self._heap: list[tuple[int, int, asyncio.Future]] = []

    @asynccontextmanager
    async def acquire(self, priority: Priority = Priority.NORMAL, label: str = ""):
        await self._enter(priority, label)
        try:
            yield
        finally:
            self._leave(label)

    async def _enter(self, priority: Priority, label: str) -> None:
        if self._active < self._max:
            self._active += 1
            logger.debug(
                "[llm-q] slot acquired (%s pri=%s active=%d/%d)",
                label,
                priority.name,
                self._active,
                self._max,
            )
            return

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._heap, (int(priority), self._seq, fut))
        logger.info(
            "[llm-q] queued (%s pri=%s waiting=%d)",
            label,
            priority.name,
            len(self._heap),
        )

        try:
            await fut
        except asyncio.CancelledError:
            self._heap = [e for e in self._heap if e[2] is not fut]
            heapq.heapify(self._heap)
            raise

    def _leave(self, label: str) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.cancelled():
                fut.set_result(None)
                logger.debug(
                    "[llm-q] slot handed off (%s → next, queue=%d)",
                    label,
                    len(self._heap),
                )
                return
        self._active -= 1
        logger.debug(
            "[llm-q] slot released (%s active=%d/%d)",
            label,
            self._active,
            self._max,
        )

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._heap)


llm_queue = LLMPriorityQueue()
