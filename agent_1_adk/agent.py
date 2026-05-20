import asyncio
import logging
import os
import sys
from datetime import date

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from google.adk.agents import LlmAgent
from google.adk.tools import load_memory
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types
from langfuse import observe

from shared.config import ADK_MODEL, LLM_BASE_URL
from shared.observability import init_langfuse

init_langfuse(service_name="orchestrator")

os.environ.setdefault("OPENAI_API_BASE", LLM_BASE_URL)
os.environ.setdefault("OPENAI_API_KEY", "lmstudio")

logger = logging.getLogger(__name__)

_TODAY = date.today().isoformat()

from .sub_agent_client import SubAgentClient

_client = SubAgentClient()


@observe(as_type="generation")
async def send_message(
    agent_name: str, task: str, tool_context: ToolContext
) -> str:
    """Delegate a task to a specialized remote investment agent.

    Call this for EACH available agent to gather their analysis,
    then synthesize all responses into a final recommendation.

    IMPORTANT: Use the exact same ticker in EVERY agent's task.
    Identify the stock ticker from the user's question first, then
    include it in all tasks you send.

    Args:
        agent_name: The exact name of the agent (e.g. "Financial RAG Agent",
            "Quant Analysis Agent", "Sentiment Intelligence Agent").
        task: Full description of the analysis. MUST include the company's
            ticker symbol (e.g. "MA", "AAPL", "NVDA") in ALL CAPS somewhere
            in the task text. Use the SAME ticker for every agent.

    Returns:
        The agent's analysis as text.
    """
    result = await _client.send_message(agent_name, task)
    return result


@observe(as_type="generation")
async def save_brief(
    ticker: str,
    recommendation: str,
    confidence: float,
    rationale: str = "",
    tool_context: ToolContext = None,
) -> str:
    """Save the final investment brief for future reference.

    Call this once after synthesizing all agent responses to persist
    the recommendation so it can be referenced in future conversations.

    Args:
        ticker: The stock ticker symbol (e.g. "NVDA", "AAPL").
        recommendation: The final recommendation (BUY, HOLD, or SELL).
        confidence: Confidence score between 0.0 and 1.0.
        rationale: Brief explanation of the recommendation.

    Returns:
        Confirmation message.
    """
    from shared.memory import PerformanceTracker, TickerMemory

    session_id = tool_context.session.id if tool_context and tool_context.session else "unknown"
    user_id = tool_context.user_id if tool_context else "default_user"

    tm = TickerMemory()
    await tm.store_minimal(
        ticker=ticker,
        user_id=user_id,
        session_id=session_id,
        query=f"Saved brief for {ticker}",
        response_text=rationale,
        recommendation=recommendation.upper(),
        confidence=confidence,
    )

    pt = PerformanceTracker()
    await pt.record_recommendation(
        ticker=ticker,
        user_id=user_id,
        recommendation=recommendation.upper(),
        confidence=confidence,
    )

    return f"Brief saved for {ticker}: {recommendation.upper()} (confidence: {confidence:.2f})"


def _build_instruction() -> str:
    agent_list = _client.list_agents()
    skill_lines = (
        "\n".join(
            f"  - {a['name']}: {a['description']}"
            for a in agent_list
        )
        if agent_list
        else "  (none discovered yet)"
    )
    return f"""\
Today's date is {_TODAY}. Use this as the reference date for all analysis.

You are an investment research orchestrator. Your job is to gather analysis
from specialized agents and produce a BUY/HOLD/SELL recommendation.

Available agents:
{skill_lines}

PROCEDURE:
1.  Identify the stock ticker from the user's question. If the user mentions
    a company name (e.g. "Mastercard", "Apple", "Microsoft"), determine its
    ticker symbol (MA, AAPL, MSFT).
2.  Call `send_message` for EVERY available agent. You MUST call all agents.
3.  Each task MUST include the SAME ticker symbol in ALL CAPS (e.g. "MA").
    Do NOT use different tickers for different agents.
4.  If the user mentions portfolio holdings (e.g. "My portfolio holds AAPL,
    MSFT, GOOGL"), you MUST include them in the task text for the Quant
    Analysis Agent. Use this exact format:
    "Analyze ORCL. My portfolio holds AAPL, MSFT, GOOGL."
5.  After all agents respond, synthesize their findings into a
    BUY/HOLD/SELL recommendation with supporting evidence.
6.  Call `save_brief` with your final recommendation to persist it
    for future reference.
7.  If the user asks about past analysis or "what did you recommend before",
    use the `load_memory` tool to search past conversations.

TASK FORMAT — always include the ticker and current date ({_TODAY}) in the task text:
  "Analyze MA (Mastercard) SEC filings as of {_TODAY} for recent financial performance."

For general chat or non-stock queries, respond conversationally.
"""


async def discover_background() -> None:
    await _client.discover()
    agent_list = _client.list_agents()
    logger.info(
        "Discovered %d agents: %s",
        len(agent_list),
        [a["name"] for a in agent_list],
    )
    root_agent.instruction = _build_instruction()


root_agent = LlmAgent(
    name="orchestrator",
    model=ADK_MODEL,
    description=(
        "Coordinates specialized investment agents into a comprehensive "
        "Investment Brief via A2A protocol"
    ),
    instruction=_build_instruction(),
    tools=[send_message, save_brief, load_memory],
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.0,
    ),
)
root_agent._sub_agent_client = _client

try:
    loop = asyncio.get_running_loop()
    loop.create_task(discover_background())
except RuntimeError:
    asyncio.run(discover_background())
