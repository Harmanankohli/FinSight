"""AG-UI bridge endpoint — routes through FinSight's ADK runner with full guardrails.

Exposes POST /a2a-agui — accepts RunAgentInput JSON, streams AG-UI events.
Uses the same runner as the RAGAS eval endpoint but adds:
- User identity resolution (X-FinSight-User-Id header or payload properties)
- Thread → ADK session mapping (thread_id = session_id)
- Off-topic guardrail (mirrors FinSightAgentExecutor)
- Today's brief cache (fast-path, no agent calls)
- Memory context injection
- Sub-agent tracking via function call introspection (STATE_DELTA active_agent)
- Cancellation via disconnect detection
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import AsyncGenerator

from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    UserMessage,
)
from google.adk.runners import Runner
from google.genai import types
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from shared.agui_sse import sse
from shared.guardrails import is_off_topic

logger = logging.getLogger(__name__)

from shared.logging_config import logged, logged_sync

_AGENT_DISPLAY_NAMES = {
    "financial rag agent": "Financial RAG Agent",
    "rag": "Financial RAG Agent",
    "quant analysis agent": "Quant Analysis Agent",
    "quant": "Quant Analysis Agent",
    "market context agent": "Market Context Agent",
    "market context": "Market Context Agent",
    "sentiment": "Market Context Agent",
}


def _display_name(agent_name: str) -> str:
    return _AGENT_DISPLAY_NAMES.get(agent_name.lower(), agent_name)


def _extract_user_text(payload: RunAgentInput) -> str:
    for msg in reversed(payload.messages or []):
        role = getattr(msg, "role", None)
        if role == "user" or isinstance(msg, UserMessage):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return ""


async def _get_today_cached_text(ticker: str, *, user_id: str | None = None) -> str | None:
    from datetime import datetime
    from shared.config import IST
    from shared.memory import TickerMemory

    tm = TickerMemory()
    latest = await tm.get_latest(ticker, user_id=user_id)
    if not latest:
        return None
    analysis_date = latest.get("analysis_date") or latest["created_at"][:10]
    today = datetime.now(IST).date().isoformat()
    if analysis_date != today:
        return None
    rec = latest.get("recommendation", "UNKNOWN")
    conf = latest.get("confidence", 0.5)
    try:
        data = json.loads(latest.get("brief_json", "{}"))
        response_text = data.get("response_text", "")
    except Exception:
        response_text = ""
    if response_text:
        return f"**{ticker} — {rec}** (confidence: {conf:.0%})\n\n{response_text}"
    if rec != "UNKNOWN":
        return f"**{ticker} — {rec}** (confidence: {conf:.0%})"
    return None


async def _build_memory_context(user_input: str, user_id: str) -> str:
    from datetime import datetime
    from shared.config import IST
    from shared.memory import PortfolioStore, TickerMemory
    from shared.ticker_utils import extract_ticker

    ticker = extract_ticker(user_input)
    parts = []

    if ticker:
        tm = TickerMemory()
        latest = await tm.get_latest(ticker, user_id=user_id)
        if latest:
            context = await tm.format_context(ticker, max_tokens=300, user_id=user_id)
            if context:
                analysis_date = latest.get("analysis_date")
                if not analysis_date:
                    raw = latest["created_at"]
                    analysis_date = raw.split("T")[0] if "T" in raw else raw[:10]
                today = datetime.now(IST).date().isoformat()
                if analysis_date == today:
                    parts.append(f"[TODAY — analysis is current, you may return it directly without calling agents again] {context}")
                else:
                    parts.append(
                        f"[STALE — analyzed on {analysis_date}, today is {today}. "
                        f"You MUST call ALL agents for a fresh analysis before responding.] {context}"
                    )

    ps = PortfolioStore()
    holdings = await ps.get_holdings(user_id)
    if holdings:
        parts.append(f"Background — user's known holdings: {', '.join(holdings)}")

    return "\n".join(parts)


_REC_RE = re.compile(r"\b(BUY|HOLD|SELL)\b", re.IGNORECASE)
_CONF_RE = re.compile(
    r"(?:confidence|conf)(?:\s+score)?[:\s]*(\d+(?:\.\d+)?)\s*%?"
    r"|(\d+(?:\.\d+)?)\s*%\s*(?:confidence|conf)",
    re.IGNORECASE,
)


async def _auto_save_brief(
    user_query: str, response_text: str, session_id: str, user_id: str
) -> None:
    """Persist the investment brief to TickerMemory after synthesis completes."""
    from datetime import datetime
    from shared.config import IST
    from shared.memory import PerformanceTracker, TickerMemory
    from shared.ticker_utils import extract_ticker

    try:
        ticker = extract_ticker(user_query)
        if not ticker:
            return

        tm = TickerMemory()
        existing = await tm.get_latest(ticker, user_id=user_id)
        if existing:
            ad = existing.get("analysis_date") or existing["created_at"][:10]
            if ad == datetime.now(IST).date().isoformat():
                stored = ""
                try:
                    bj = json.loads(existing.get("brief_json", "{}"))
                    stored = bj.get("response_text", "")
                except Exception:
                    logger.debug("Could not parse brief_json", exc_info=True)
                if len(response_text) > len(stored):
                    await tm.update_response_text(existing["id"], response_text)
                return

        rec_m = _REC_RE.search(response_text)
        rec = rec_m.group(1).upper() if rec_m else "UNKNOWN"
        conf = 0.5
        conf_m = _CONF_RE.search(response_text)
        if conf_m:
            raw = float(conf_m.group(1) or conf_m.group(2))
            conf = raw / 100.0 if raw > 1.0 else raw

        await tm.store_minimal(
            ticker=ticker, user_id=user_id, session_id=session_id,
            query=user_query, response_text=response_text,
            recommendation=rec, confidence=round(conf, 2),
        )

        pt = PerformanceTracker()
        await pt.record_recommendation(
            ticker=ticker, user_id=user_id,
            recommendation=rec, confidence=round(conf, 2),
        )
        logger.info("Auto-saved brief: %s %s (%.0f%%)", ticker, rec, conf * 100)
    except Exception:
        logger.debug("Auto-save brief failed", exc_info=True)


async def _stream(
    runner: Runner,
    user_text: str,
    thread_id: str,
    run_id: str,
    user_id: str,
    request: Request,
    payload: RunAgentInput,
) -> AsyncGenerator[str, None]:
    session_id = thread_id
    msg_id = str(uuid.uuid4())
    text_started = False
    # Map call_id → agent_display_name for matching responses back to agents
    pending_calls: dict[str, str] = {}  # call_id → agent_display_name
    # Also track fn_call.id → call_id for ADK's internal ID scheme
    pending_by_fn_id: dict[str, str] = {}
    active_agents: list[str] = []
    synthesis_parts: list[str] = []
    had_send_message = False

    yield sse(RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
    ))

    # Seed the state so JSON Patch operations resolve against a known root
    yield sse(StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        snapshot={"active_agents": [], "active_agent": None},
    ))

    # Off-topic guardrail
    if is_off_topic(user_text):
        rejection = "I'm specialized in investment research. Please ask about stocks, portfolios, or financial analysis."
        yield sse(TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant"))
        yield sse(TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=rejection))
        yield sse(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
        yield sse(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id))
        return

    # Today's brief cache check
    from shared.ticker_utils import extract_ticker
    ticker_hint = extract_ticker(user_text) or "unknown"
    if ticker_hint != "unknown":
        cached = await _get_today_cached_text(ticker_hint, user_id=user_id)
        if cached:
            yield sse(TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant"))
            chunk_size = 500
            for i in range(0, len(cached), chunk_size):
                yield sse(TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=cached[i:i+chunk_size]))
            yield sse(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
            yield sse(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id))
            return

    # Build memory context for prompt injection
    memory_ctx = await _build_memory_context(user_text, user_id)
    enriched_input = f"[MEMORY CONTEXT]\n{memory_ctx}\n[/MEMORY CONTEXT]\n\n{user_text}" if memory_ctx else user_text

    # Ensure ADK session exists for this thread
    existing = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id,
    )
    if existing is None:
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id,
        )

    content = types.Content(role="user", parts=[types.Part.from_text(text=enriched_input)])

    yield sse(StepStartedEvent(type=EventType.STEP_STARTED, step_name="orchestrator"))

    try:
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            # Function calls → sub-agent delegations
            for fn_call in event.get_function_calls():
                call_id = str(uuid.uuid4())
                fn_id = getattr(fn_call, "id", None) or fn_call.name
                pending_calls[call_id] = fn_call.name
                pending_by_fn_id[fn_id] = call_id

                if fn_call.name == "send_message":
                    had_send_message = True
                    args = dict(fn_call.args or {})
                    agent_raw = args.get("agent_name", "")
                    agent_display = _display_name(agent_raw)
                    if agent_display not in active_agents:
                        active_agents.append(agent_display)
                    yield sse(StateDeltaEvent(
                        type=EventType.STATE_DELTA,
                        delta=[
                            {"op": "replace", "path": "/active_agents", "value": list(active_agents)},
                            {"op": "replace", "path": "/active_agent", "value": agent_display},
                        ],
                    ))

                yield sse(ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START,
                    tool_call_id=call_id,
                    tool_call_name=fn_call.name,
                    parent_message_id=msg_id,
                ))
                yield sse(ToolCallArgsEvent(
                    type=EventType.TOOL_CALL_ARGS,
                    tool_call_id=call_id,
                    delta=json.dumps(dict(fn_call.args or {})),
                ))
                yield sse(ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=call_id))

            # Function responses → sub-agent results
            for fn_resp in event.get_function_responses():
                fn_id = getattr(fn_resp, "id", None) or fn_resp.name
                call_id = pending_by_fn_id.get(fn_id, str(uuid.uuid4()))
                raw = fn_resp.response
                if isinstance(raw, str):
                    result_text = raw
                elif isinstance(raw, (dict, list, int, float, bool, type(None))):
                    result_text = json.dumps(raw)
                elif hasattr(raw, "model_dump"):
                    result_text = json.dumps(raw.model_dump())
                else:
                    result_text = str(raw)
                yield sse(ToolCallResultEvent(
                    type=EventType.TOOL_CALL_RESULT,
                    message_id=str(uuid.uuid4()),
                    tool_call_id=call_id,
                    content=result_text,
                    role="tool",
                ))

            # Text content → final synthesis
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        if not text_started:
                            yield sse(TextMessageStartEvent(
                                type=EventType.TEXT_MESSAGE_START,
                                message_id=msg_id,
                                role="assistant",
                            ))
                            text_started = True
                        synthesis_parts.append(part.text)
                        yield sse(TextMessageContentEvent(
                            type=EventType.TEXT_MESSAGE_CONTENT,
                            message_id=msg_id,
                            delta=part.text,
                        ))

        if text_started:
            yield sse(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))

        if active_agents:
            yield sse(StateDeltaEvent(
                type=EventType.STATE_DELTA,
                delta=[
                    {"op": "replace", "path": "/active_agents", "value": []},
                    {"op": "replace", "path": "/active_agent", "value": None},
                ],
            ))

        yield sse(StepFinishedEvent(type=EventType.STEP_FINISHED, step_name="orchestrator"))

        if had_send_message and synthesis_parts:
            asyncio.create_task(
                _auto_save_brief(user_text, "".join(synthesis_parts), session_id, user_id)
            )

    except asyncio.CancelledError:
        logger.info("Bridge stream cancelled run_id=%s", run_id)
        yield sse(RunErrorEvent(type=EventType.RUN_ERROR, message="Request cancelled", code=None))
    except Exception as exc:
        logger.exception("AG-UI bridge error run_id=%s", run_id)
        yield sse(RunErrorEvent(type=EventType.RUN_ERROR, message=str(exc), code=None))

    yield sse(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id))


def make_agui_bridge_endpoint(runner: Runner):
    """Return a Starlette POST handler for /a2a-agui."""

    async def a2a_agui(request: Request):
        try:
            body = await request.json()
            payload = RunAgentInput.model_validate(body)
        except Exception as exc:
            return JSONResponse({"error": f"Invalid request: {exc}"}, status_code=400)

        thread_id = payload.thread_id or str(uuid.uuid4())
        run_id = payload.run_id or str(uuid.uuid4())

        user_id = (
            request.headers.get("X-FinSight-User-Id")
            or (payload.forwarded_props or {}).get("user_id")
        )
        if not user_id:
            user_id = f"anon-{uuid.uuid4()}"

        user_text = _extract_user_text(payload)
        if not user_text:
            return JSONResponse({"error": "No user message in payload"}, status_code=400)

        response_headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        if user_id.startswith("anon-"):
            response_headers["X-FinSight-User-Id"] = user_id

        return StreamingResponse(
            _stream(runner, user_text, thread_id, run_id, user_id, request, payload),
            media_type="text/event-stream",
            headers=response_headers,
        )

    return a2a_agui
