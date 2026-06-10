"""Tests that save_brief stores the full synthesis text, not the short rationale."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub out heavy ADK / langfuse packages before agent_1_adk.agent is imported.
# ---------------------------------------------------------------------------
def _stub_modules():
    sub_client_mock = MagicMock()
    sub_client_mock.SubAgentClient = MagicMock
    obs_mock = MagicMock()
    obs_mock.init_langfuse = MagicMock()
    stubs = {
        "langfuse": MagicMock(),
        "langfuse.span_filter": MagicMock(),
        "shared.observability": obs_mock,
        "agent_1_adk.sub_agent_client": sub_client_mock,
    }
    for name, mock in stubs.items():
        sys.modules.setdefault(name, mock)


_stub_modules()


def _make_event(author: str, text: str | None = None, has_fn_call: bool = False):
    """Build a minimal fake ADK event."""
    event = MagicMock()
    event.author = author
    text_part = MagicMock()
    text_part.text = text
    fn_part = MagicMock(spec=[])  # no .text attribute
    parts = []
    if text is not None:
        parts.append(text_part)
    if has_fn_call:
        parts.append(fn_part)
    content = MagicMock()
    content.parts = parts
    event.content = content
    return event


def _make_tool_context(events):
    ctx = MagicMock()
    ctx.session.events = events
    ctx.session.id = "sess-test"
    ctx.user_id = "user-test"
    return ctx


LONG_SYNTHESIS = "A" * 2000
SHORT_RATIONALE = "Short summary under 100 chars."


@pytest.mark.asyncio
async def test_save_brief_uses_synthesis_over_rationale(tmp_path):
    """save_brief stores the long synthesis from session events."""
    from agent_1_adk.agent import save_brief
    from shared.memory.ticker_memory import TickerMemory

    events = [
        _make_event("user", "Analyze WMT"),
        _make_event("orchestrator", LONG_SYNTHESIS, has_fn_call=True),
    ]
    ctx = _make_tool_context(events)

    import shared.memory.store as store_mod

    db = tmp_path / "test.db"
    store_mod._db_conn = None

    pt_mock = MagicMock()
    pt_mock.record_recommendation = AsyncMock()
    with (
        patch("shared.memory.TickerMemory", lambda: TickerMemory(db_path=db)),
        patch("shared.memory.PerformanceTracker", return_value=pt_mock),
        patch("agent_1_adk.agent.asyncio.create_task"),
    ):
        await save_brief(
            ticker="WMT",
            recommendation="HOLD",
            confidence=0.75,
            rationale=SHORT_RATIONALE,
            tool_context=ctx,
        )

    store_mod._db_conn = None
    tm = TickerMemory(db_path=db)
    latest = await tm.get_latest("WMT", user_id=None)
    assert latest is not None
    data = json.loads(latest["brief_json"])
    stored_text = data.get("response_text", "")
    assert len(stored_text) == len(LONG_SYNTHESIS), (
        f"Expected synthesis length {len(LONG_SYNTHESIS)}, got {len(stored_text)}"
    )


@pytest.mark.asyncio
async def test_save_brief_falls_back_to_rationale_on_empty_session(tmp_path):
    """When session has no model events, rationale is used as fallback."""
    from agent_1_adk.agent import save_brief
    from shared.memory.ticker_memory import TickerMemory

    events = [_make_event("user", "Analyze WMT")]
    ctx = _make_tool_context(events)

    import shared.memory.store as store_mod

    db = tmp_path / "test.db"
    store_mod._db_conn = None

    pt_mock = MagicMock()
    pt_mock.record_recommendation = AsyncMock()
    with (
        patch("shared.memory.TickerMemory", lambda: TickerMemory(db_path=db)),
        patch("shared.memory.PerformanceTracker", return_value=pt_mock),
        patch("agent_1_adk.agent.asyncio.create_task"),
    ):
        await save_brief(
            ticker="WMT",
            recommendation="BUY",
            confidence=0.8,
            rationale=SHORT_RATIONALE,
            tool_context=ctx,
        )

    store_mod._db_conn = None
    tm = TickerMemory(db_path=db)
    latest = await tm.get_latest("WMT", user_id=None)
    assert latest is not None
    data = json.loads(latest["brief_json"])
    stored_text = data.get("response_text", "")
    assert stored_text == SHORT_RATIONALE
