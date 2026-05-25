import asyncio
import logging
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from agent_1_adk.agent import root_agent
from shared.config import EVAL_ENABLED
from shared.runtime_eval import score_response as _eval_score_response

__all__ = ["root_agent"]

logger = logging.getLogger(__name__)
_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOGS_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOGS_DIR / "memory_callback.log"


def _extract_query_and_response(events) -> tuple[str, str]:
    """Pull first user message and final agent text from session events."""
    user_query = ""
    response_text = ""
    for event in events:
        author = getattr(event, "author", None)
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        text = "".join(p.text for p in content.parts if getattr(p, "text", None))
        if not text:
            continue
        if not user_query and author == "user":
            user_query = text
        if author and author != "user":
            response_text = text
    return user_query, response_text


def _is_analysis_turn(events) -> bool:
    """True if the current turn produced a fresh analysis (called save_brief).

    Walks back to the most recent user message and checks whether any tool
    call after it was save_brief. A turn that only calls load_memory (i.e.
    the user is asking about past recommendations) returns False.
    """
    last_user_idx = -1
    for i in range(len(events) - 1, -1, -1):
        if getattr(events[i], "author", None) == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return False
    for event in events[last_user_idx + 1:]:
        try:
            for fn_call in event.get_function_calls():
                if fn_call.name == "save_brief":
                    return True
        except Exception:
            continue
    return False


async def _persist_memory_callback(callback_context) -> None:
    """Persist session to memory after each agent turn.

    This callback is invoked by ADK after every agent invocation.
    It stores the session's events into the memory service so that
    `load_memory` can search across all past sessions.
    """
    session = callback_context.session
    logger.info("_persist_memory_callback: session=%s, events=%s",
                session.id if session else "None",
                len(session.events) if session and session.events else 0)

    with open(_LOG_FILE, "a") as f:
        f.write(f"Callback invoked: session={session.id if session else 'None'}, events={len(session.events) if session else 0}\n")

    if not session or not session.events:
        logger.warning("No session or no events to persist")
        with open(_LOG_FILE, "a") as f:
            f.write("  No session or no events\n")
        return

    # Skip persist/eval on non-analysis turns (e.g. user asking "what were my
    # last recommendations?" which only calls load_memory). Otherwise the
    # conversational query gets indexed and pollutes future memory searches.
    if not _is_analysis_turn(session.events):
        logger.info("Skipping persist + eval — turn did not call save_brief")
        with open(_LOG_FILE, "a") as f:
            f.write("  Skipped (non-analysis turn)\n")
        return

    # Use ADK's Context.add_events_to_memory with correct signature
    try:
        await callback_context.add_events_to_memory(
            events=session.events,
            custom_metadata={
                "user_id": session.user_id,
                "session_id": session.id,
                "app_name": session.app_name,
            },
        )
        logger.info("Session persisted via add_events_to_memory (%d events)", len(session.events))
        with open(_LOG_FILE, "a") as f:
            f.write(f"  Persisted via add_events_to_memory ({len(session.events)} events)\n")
    except Exception as e:
        logger.error("add_events_to_memory failed: %s", e, exc_info=True)
        with open(_LOG_FILE, "a") as f:
            f.write(f"  add_events_to_memory failed: {e}\n")

    # ── Orchestrator RAGAS eval ─────────────────────────────────────────
    # ADK Web bypasses FinSightAgentExecutor, so the eval hook lives here.
    if EVAL_ENABLED:
        user_query, response_text = _extract_query_and_response(session.events)
        if user_query and response_text:
            trace_id = None
            try:
                from shared.observability import get_langfuse_client
                trace_id = get_langfuse_client().get_current_trace_id()
            except Exception:
                pass
            asyncio.create_task(_eval_score_response(user_query, response_text, trace_id))
            logger.info("Orchestrator eval scheduled (trace=%s)", trace_id)


root_agent.after_agent_callback = _persist_memory_callback
