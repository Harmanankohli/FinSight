import json
import logging
import os

from google.adk.agents import LlmAgent

from shared.config import ADK_MODEL, LLM_BASE_URL

from .sub_agent_client import SubAgentClient

os.environ.setdefault("OPENAI_API_BASE", LLM_BASE_URL)
os.environ.setdefault("OPENAI_API_KEY", "ollama")

logger = logging.getLogger(__name__)

# ── Discovery at module level ─────────────────────────────────────

_client = SubAgentClient()
_client.discover_sync()
_agent_list = _client.list_agents()

logger.info(
    "Discovered %d agents: %s",
    len(_agent_list),
    [a["name"] for a in _agent_list],
)

# ── One ADK tool per discovered A2A remote agent ──────────────────

def _make_agent_tool(agent_name: str, description: str):
    fn_name = agent_name.lower().replace(" ", "_").replace("-", "_")

    async def tool_fn(ticker: str) -> str:
        return await _client.send_message(
            agent_name, f"Research and analyze {ticker}"
        )

    tool_fn.__name__ = fn_name
    tool_fn.__qualname__ = fn_name
    tool_fn.__doc__ = description
    return tool_fn


_tools = [
    _make_agent_tool(a["name"], a["description"])
    for a in _agent_list
]

_SKILL_LINES = "\n".join(
    f"  - {a['name']}: {a['description']}"
    for a in _agent_list
) if _agent_list else "  (none discovered)"

_INSTRUCTION = f"""\
You are an investment research orchestrator.

Available agent tools (call ALL of them for stock queries):
{_SKILL_LINES}

When the user asks about a stock (e.g. "NVDA", "msft", "AAPL"),
call EVERY available tool with the ticker parameter. Each tool
provides a different type of analysis — you need ALL their results.

Synthesize the results into a BUY/HOLD/SELL recommendation.

For general chat or non-stock queries, respond conversationally.
"""

root_agent = LlmAgent(
    name="orchestrator",
    model=ADK_MODEL,
    description=(
        "Coordinates specialized investment agents into a comprehensive "
        "Investment Brief"
    ),
    instruction=_INSTRUCTION,
    tools=_tools,
)
