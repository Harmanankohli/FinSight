"""AG-UI compatible SSE endpoint for RAGAS evaluation.

Exposes POST /agentic_chat — accepts RunAgentInput JSON, streams AG-UI events
backed by the existing ADK Runner so RAGAS can collect responses and tool call
trajectories for offline batch evaluation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
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

logger = logging.getLogger(__name__)

_EVAL_USER_ID = "eval_user"


from shared.agui_sse import sse as _sse


# Stream AG-UI protocol: RunStarted → tool calls / text chunks → RunFinished
async def _stream(
    runner: Runner,
    user_text: str,
    thread_id: str,
    run_id: str,
    payload: RunAgentInput,
) -> AsyncGenerator[str, None]:
    session_id = run_id

    existing = await runner.session_service.get_session(
        app_name=runner.app_name,
        user_id=_EVAL_USER_ID,
        session_id=session_id,
    )
    if existing is None:
        await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id=_EVAL_USER_ID,
            session_id=session_id,
        )

    yield _sse(RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=thread_id,
        run_id=run_id,
        input=payload.model_dump(by_alias=True),
    ))

    content = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
    msg_id = str(uuid.uuid4())
    text_started = False

    # Track active tool call IDs so function responses can reference their parent calls
    pending_calls: dict[str, str] = {}  # fn_name → call_id

    try:
        async for event in runner.run_async(
            user_id=_EVAL_USER_ID,
            session_id=session_id,
            new_message=content,
        ):
            # ── Function calls (sub-agent delegations) ───────────────────────
            for fn_call in event.get_function_calls():
                call_id = str(uuid.uuid4())
                pending_calls[fn_call.name] = call_id
                yield _sse(ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START,
                    tool_call_id=call_id,
                    tool_call_name=fn_call.name,
                    parent_message_id=msg_id,
                ))
                yield _sse(ToolCallArgsEvent(
                    type=EventType.TOOL_CALL_ARGS,
                    tool_call_id=call_id,
                    delta=json.dumps(dict(fn_call.args or {})),
                ))
                yield _sse(ToolCallEndEvent(
                    type=EventType.TOOL_CALL_END,
                    tool_call_id=call_id,
                ))

            # ── Function responses (sub-agent results) ───────────────────────
            for fn_resp in event.get_function_responses():
                call_id = pending_calls.get(fn_resp.name, str(uuid.uuid4()))
                result_text = ""
                if fn_resp.response:
                    result_text = (
                        fn_resp.response
                        if isinstance(fn_resp.response, str)
                        else json.dumps(fn_resp.response)
                    )
                yield _sse(ToolCallResultEvent(
                    type=EventType.TOOL_CALL_RESULT,
                    message_id=str(uuid.uuid4()),
                    tool_call_id=call_id,
                    content=result_text,
                    role="tool",
                ))

            # ── Text content ─────────────────────────────────────────────────
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        if not text_started:
                            yield _sse(TextMessageStartEvent(
                                type=EventType.TEXT_MESSAGE_START,
                                message_id=msg_id,
                                role="assistant",
                            ))
                            text_started = True
                        yield _sse(TextMessageContentEvent(
                            type=EventType.TEXT_MESSAGE_CONTENT,
                            message_id=msg_id,
                            delta=part.text,
                        ))

        if text_started:
            yield _sse(TextMessageEndEvent(
                type=EventType.TEXT_MESSAGE_END,
                message_id=msg_id,
            ))

    except Exception as exc:
        logger.exception("AG-UI stream error for run_id=%s", run_id)
        yield _sse(RunErrorEvent(
            type=EventType.RUN_ERROR,
            message=str(exc),
            code=None,
        ))

    yield _sse(RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id=thread_id,
        run_id=run_id,
    ))


def make_agui_endpoint(runner: Runner):
    """Return a Starlette POST handler for /agentic_chat."""

    async def agentic_chat(request: Request):
        try:
            body = await request.json()
            payload = RunAgentInput.model_validate(body)
        except Exception as exc:
            return JSONResponse({"error": f"Invalid request: {exc}"}, status_code=400)

        thread_id = payload.thread_id or str(uuid.uuid4())
        run_id = payload.run_id or str(uuid.uuid4())

        user_text = ""
        for msg in reversed(payload.messages or []):
            role = getattr(msg, "role", None)
            if role == "user" or isinstance(msg, UserMessage):
                user_text = (
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
                break

        if not user_text:
            return JSONResponse({"error": "No user message in payload"}, status_code=400)

        return StreamingResponse(
            _stream(runner, user_text, thread_id, run_id, payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return agentic_chat
