import logging
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from agent_1_adk.agent import root_agent

__all__ = ["root_agent"]

logger = logging.getLogger(__name__)
_LOG_FILE = Path(__file__).resolve().parent.parent.parent / "memory_callback.log"


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


root_agent.after_agent_callback = _persist_memory_callback
