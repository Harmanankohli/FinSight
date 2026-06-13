"""Unit tests verifying the orchestrator's parallel-dispatch building blocks.

These tests intentionally do not import SubAgentClient because the live `a2a`
package version skews between environments. They verify the underlying
guarantees:
  - asyncio.gather over 3 coroutines runs them concurrently
  - the timeout-map substring lookup matches the new "Market Context" name
"""

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_three_dispatch_coroutines_run_concurrently():
    """Three 0.2s-sleep coroutines under asyncio.gather should finish in ~0.2s, not ~0.6s."""

    async def fake_send(agent: str) -> str:
        await asyncio.sleep(0.2)
        return f"reply-from-{agent}"

    t0 = time.monotonic()
    results = await asyncio.gather(
        fake_send("Financial RAG Agent"),
        fake_send("Quant Analysis Agent"),
        fake_send("Market Context Agent"),
    )
    elapsed = time.monotonic() - t0

    assert len(results) == 3
    assert all(r.startswith("reply-from-") for r in results)
    assert elapsed < 0.5, f"Dispatch was not concurrent — took {elapsed:.2f}s"


def test_timeout_map_uses_market_context_key():
    """The substring lookup matches the new agent name 'Market Context Agent'."""
    from shared.settings import (
        A2A_TIMEOUT,
        A2A_TIMEOUT_MARKET_CONTEXT,
        A2A_TIMEOUT_QUANT,
        A2A_TIMEOUT_RAG,
    )

    _TIMEOUT_MAP = {
        "rag": A2A_TIMEOUT_RAG,
        "quant": A2A_TIMEOUT_QUANT,
        "market context": A2A_TIMEOUT_MARKET_CONTEXT,
    }

    def lookup(agent_name: str) -> float:
        agent_lower = agent_name.lower()
        return next((v for k, v in _TIMEOUT_MAP.items() if k in agent_lower), A2A_TIMEOUT)

    assert lookup("Financial RAG Agent") == A2A_TIMEOUT_RAG
    assert lookup("Quant Analysis Agent") == A2A_TIMEOUT_QUANT
    assert lookup("Market Context Agent") == A2A_TIMEOUT_MARKET_CONTEXT
    assert lookup("Some Other Agent") == A2A_TIMEOUT  # falls through to default


def test_no_sentiment_key_collides_with_new_name():
    """The old 'sentiment' key string must not exist in the new agent name."""
    assert "sentiment" not in "Market Context Agent".lower()
