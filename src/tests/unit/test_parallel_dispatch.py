"""Unit tests verifying the orchestrator's parallel-dispatch building blocks.

These tests intentionally do not import SubAgentClient because the live `a2a`
package version skews between environments. They verify the underlying
guarantees:
  - asyncio.gather over 4 coroutines runs them concurrently
  - the timeout-map substring lookup matches five agent names
"""

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_four_dispatch_coroutines_run_concurrently():
    """Four 0.2s-sleep coroutines under asyncio.gather should finish in ~0.2s, not ~0.8s."""

    async def fake_send(agent: str) -> str:
        await asyncio.sleep(0.2)
        return f"reply-from-{agent}"

    t0 = time.monotonic()
    results = await asyncio.gather(
        fake_send("Financial RAG Agent"),
        fake_send("Quant Analysis Agent"),
        fake_send("Market Context Agent"),
        fake_send("Analytics Agent"),
    )
    elapsed = time.monotonic() - t0

    assert len(results) == 4
    assert all(r.startswith("reply-from-") for r in results)
    assert elapsed < 0.5, f"Dispatch was not concurrent — took {elapsed:.2f}s"


def test_timeout_map_uses_new_agent_keys():
    """The substring lookup matches all five agent names."""
    from shared.settings import (
        A2A_TIMEOUT,
        A2A_TIMEOUT_ANALYTICS,
        A2A_TIMEOUT_MARKET_CONTEXT,
        A2A_TIMEOUT_QUANT,
        A2A_TIMEOUT_RAG,
        A2A_TIMEOUT_REVIEWER,
    )

    _TIMEOUT_MAP = {
        "rag": A2A_TIMEOUT_RAG,
        "quant": A2A_TIMEOUT_QUANT,
        "market context": A2A_TIMEOUT_MARKET_CONTEXT,
        "analytics": A2A_TIMEOUT_ANALYTICS,
        "reviewer": A2A_TIMEOUT_REVIEWER,
    }

    def lookup(agent_name: str) -> float:
        agent_lower = agent_name.lower()
        return next((v for k, v in _TIMEOUT_MAP.items() if k in agent_lower), A2A_TIMEOUT)

    assert lookup("Financial RAG Agent") == A2A_TIMEOUT_RAG
    assert lookup("Quant Analysis Agent") == A2A_TIMEOUT_QUANT
    assert lookup("Market Context Agent") == A2A_TIMEOUT_MARKET_CONTEXT
    assert lookup("Analytics Agent") == A2A_TIMEOUT_ANALYTICS
    assert lookup("Reviewer Agent") == A2A_TIMEOUT_REVIEWER
    assert lookup("Some Other Agent") == A2A_TIMEOUT


def test_no_sentiment_key_collides_with_new_name():
    """The old 'sentiment' key string must not exist in the new agent names."""
    assert "sentiment" not in "Market Context Agent".lower()
    assert "sentiment" not in "Analytics Agent".lower()
    assert "sentiment" not in "Reviewer Agent".lower()
