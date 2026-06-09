"""Deferred eval gate — holds sub-agent evals until the orchestrator releases them.

Sub-agents produce responses before the orchestrator synthesises the final
answer.  Firing eval LLM calls immediately starves the orchestrator's
synthesis because all processes share a single LM Studio instance and the
process-local priority queue cannot coordinate across processes.

This module provides a per-process deferred queue:

    from shared.eval_gate import defer_eval, release_evals

    # In sub-agent executor (replaces asyncio.create_task):
    defer_eval(score_quant_response, query, response, result, trace_id)

    # Called by orchestrator via POST /release-evals after synthesis:
    await release_evals()

A safety-net auto-release fires after EVAL_DEFER_TIMEOUT seconds so evals
still run even if the orchestrator never calls /release-evals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from shared.logging_config import logged

logger = logging.getLogger(__name__)

EVAL_DEFER_TIMEOUT = 120.0  # seconds — safety net

_deferred: list[tuple[Callable[..., Coroutine], tuple, dict]] = []
_auto_release_task: asyncio.Task | None = None


def defer_eval(fn: Callable[..., Coroutine], *args: Any, **kwargs: Any) -> None:
    """Queue an eval coroutine for deferred execution."""
    global _auto_release_task
    _deferred.append((fn, args, kwargs))
    logger.info("[eval-gate] deferred %s (queue=%d)", fn.__name__, len(_deferred))

    loop = asyncio.get_event_loop()
    if _auto_release_task is None or _auto_release_task.done():
        _auto_release_task = loop.create_task(_auto_release())


async def release_evals() -> int:
    """Fire all deferred evals. Returns the count of evals released."""
    global _auto_release_task
    if _auto_release_task and not _auto_release_task.done():
        _auto_release_task.cancel()
        _auto_release_task = None

    batch = list(_deferred)
    _deferred.clear()
    if not batch:
        return 0

    logger.info("[eval-gate] releasing %d deferred eval(s)", len(batch))
    for fn, args, kwargs in batch:
        asyncio.create_task(fn(*args, **kwargs))
    return len(batch)


async def _auto_release() -> None:
    """Safety net: release evals after timeout if orchestrator never signals."""
    try:
        await asyncio.sleep(EVAL_DEFER_TIMEOUT)
        n = await release_evals()
        if n:
            logger.warning("[eval-gate] auto-released %d eval(s) after %.0fs timeout", n, EVAL_DEFER_TIMEOUT)
    except asyncio.CancelledError:
        pass
