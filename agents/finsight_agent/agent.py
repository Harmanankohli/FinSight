import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from agent_1_adk.agent import root_agent

__all__ = ["root_agent"]


async def _persist_memory_callback(callback_context) -> None:
    """Persist session to memory after each agent turn.

    This callback is invoked by ADK after every agent invocation.
    It stores the session's events into the memory service so that
    `load_memory` can search across all past sessions.
    """
    session = callback_context.session
    if session and session.events:
        try:
            await callback_context.add_session_to_memory(session)
        except Exception:
            pass  # Memory persistence is best-effort


root_agent.after_agent_callback = _persist_memory_callback
