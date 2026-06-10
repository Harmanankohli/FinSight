import asyncio
import json
import logging
import sys
from datetime import datetime

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types
from shared.settings import ADK_MODEL, IST
from shared.logging_config import logged, logged_sync

logger = logging.getLogger(__name__)

from .sub_agent_client import SubAgentClient

_client = SubAgentClient()


# ADK tool function: delegates tasks to sub-agents via A2A
async def send_message(
    agent_name: str, task: str, tool_context: ToolContext
) -> str:
    """Delegate a task to a specialized remote investment agent.

    ONLY call this tool when agents are listed under "Available agents"
    in your instructions. Call it for EACH listed agent. Use the EXACT
    agent name from that list — never invent or guess names.

    If no agents are listed, DO NOT call this tool — there are no agents.

    Args:
        agent_name: The exact name of the agent as listed under
            "Available agents" in your instructions. Never invent names.
        task: Full description of the analysis. MUST include the company's
            ticker symbol (e.g. "MA", "AAPL", "NVDA") in ALL CAPS somewhere
            in the task text. Use the SAME ticker for every agent.

    Returns:
        The agent's analysis as text.
    """
    if not _client.list_agents():
        return json.dumps({
            "error": "No agents are currently available. They may still be "
                     "starting up. Do not call this tool — answer based on "
                     "your own knowledge instead."
        })
    resolved = _client.resolve_agent_name(agent_name)
    if resolved is None:
        valid = [a["name"] for a in _client.list_agents()]
        return json.dumps({
            "error": f"Unknown agent '{agent_name}'. Valid agents are: {valid}. "
                     "Use one of these exact names and retry."
        })
    result = await _client.send_message(resolved, task)
    return result


def _synthesis_text_from_context(tool_context) -> str:
    """Return the longest LLM text from the current turn in session.events."""
    try:
        events = tool_context.session.events
    except AttributeError:
        return ""
    last_user_idx = -1
    for i in range(len(events) - 1, -1, -1):
        if getattr(events[i], "author", None) == "user":
            last_user_idx = i
            break
    best = ""
    for event in events[last_user_idx + 1:]:
        author = getattr(event, "author", None)
        if not author or author == "user":
            continue
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        text = "".join(
            p.text for p in content.parts if getattr(p, "text", None)
        )
        if len(text) > len(best):
            best = text
    return best


# Dedup: skip if brief for this ticker was already saved today
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

    # Skip if a brief for this ticker was already saved today (dedup)
    existing = await tm.get_latest(ticker, user_id=user_id)
    if existing:
        ad = existing.get("analysis_date") or existing["created_at"][:10]
        if ad == datetime.now(IST).date().isoformat():
            return (
                f"Brief already saved today for {ticker}: "
                f"{existing['recommendation']} (confidence: {existing['confidence']:.2f})"
            )

    synthesis = _synthesis_text_from_context(tool_context) if tool_context else ""
    response_text = synthesis if len(synthesis) > len(rationale) else rationale

    await tm.store_minimal(
        ticker=ticker,
        user_id=user_id,
        session_id=session_id,
        query=f"Saved brief for {ticker}",
        response_text=response_text,
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

    asyncio.create_task(_evaluate_past_recommendations(ticker))

    return f"Brief saved for {ticker}: {recommendation.upper()} (confidence: {confidence:.2f})"


async def load_memory(query: str, tool_context: ToolContext = None) -> str:
    """Search past conversations and saved briefs.

    Args:
        query: The search query to find relevant memories.

    Returns:
        Matching memory entries as text.
    """
    try:
        response = await tool_context.search_memory(query)
        if not response or not response.memories:
            return "No memories found."
        parts = []
        for mem in response.memories:
            for ev in getattr(mem, "events", []):
                for part in getattr(getattr(ev, "content", None), "parts", []) or []:
                    if getattr(part, "text", None):
                        parts.append(part.text)
        return "\n---\n".join(parts) if parts else "No relevant memories found."
    except Exception as e:
        logger.warning("load_memory failed: %s", e)
        return f"Memory search unavailable: {e}"


# Fire-and-forget: evaluate past recommendations vs current prices without blocking the response
async def _evaluate_past_recommendations(ticker: str) -> None:
    """Background task: evaluate past recommendations against current prices."""
    try:
        from shared.memory import PerformanceTracker as _PT
        pt = _PT()
        results = await pt.evaluate_all()
        if results:
            logger.info("Evaluated %d past recommendations for %s", len(results), ticker)
    except Exception:
        logger.debug("Background evaluation failed for %s", ticker, exc_info=True)


_STATIC_PREAMBLE = """\
You are an investment research orchestrator. Your job is to gather analysis
from specialized agents and produce a BUY/HOLD/SELL recommendation.

PROCEDURE — follow these steps IN ORDER:
1.  Identify the stock ticker from the user's question. If the user mentions
    a company name (e.g. "Mastercard", "Apple", "Microsoft"), determine its
    ticker symbol (MA, AAPL, MSFT).
2.  YOUR VERY FIRST ACTION must be to call `send_message` for EVERY agent
    listed under "Available agents" below. Do NOT call `load_memory` first.
    Do NOT call any other tool first. You MUST emit ALL `send_message` calls
    in a SINGLE assistant response so they execute in PARALLEL.
    Use their EXACT names — never invent agent names.
3.  Each task MUST include the SAME ticker symbol in ALL CAPS (e.g. "MA").
    Do NOT use different tickers for different agents.
4.  Only include portfolio holdings in the Quant Analysis Agent task if the
    user EXPLICITLY mentions their portfolio or asks for correlation/portfolio
    analysis in their CURRENT message (e.g. "My portfolio holds AAPL, MSFT").
    Do NOT include holdings from memory context background lines — those are
    for your reference only. If the user just asks about a single stock, send
    only that ticker to the Quant agent.
5.  After ALL agents have responded (you will receive their results together in
    the next turn), synthesize their findings into a BUY/HOLD/SELL
    recommendation with supporting evidence. Include a confidence score
    (0.0–1.0) in your response. Your analysis is automatically saved after
    you respond — no manual save step needed.

TOOL RULES:
- `send_message`: Use this for ALL stock analysis requests. ALWAYS call it first.
- `load_memory`: ONLY use this when the user explicitly asks about past
  recommendations (e.g. "what did you recommend before", "show history").
  NEVER call load_memory when the user asks to analyze a stock.

MEMORY CONTEXT RULES (applies when [MEMORY CONTEXT] block is present):
- [TODAY]: analysis was done today — you MUST return it directly without calling agents again.
- [STALE]: analysis is from a prior day — you MUST call ALL agents for a fresh analysis.
  Treat stale data as background reference only. Do NOT return it as the current recommendation.

TASK FORMAT — always include the ticker and current date in the task text:
  "Analyze MA (Mastercard) SEC filings for recent financial performance."

For general chat or non-stock queries, respond conversationally.\
"""

_STATIC_PREAMBLE_FALLBACK = """\
You are an investment research orchestrator. Your job is to gather analysis
from specialized agents and produce a BUY/HOLD/SELL recommendation.

NOTE: No specialized agents are currently available — they may still be
starting up. Do NOT call `send_message` because there are no agents to
contact. Never invent agent names or make up agents.

PROCEDURE:
1.  Identify the stock ticker from the user's question. If the user mentions
    a company name (e.g. "Mastercard", "Apple", "Microsoft"), determine its
    ticker symbol (MA, AAPL, MSFT).
2.  Provide your best analysis based on your own general knowledge.
    Include a BUY/HOLD/SELL recommendation with a confidence score (0.0–1.0).
    Your analysis is automatically saved after you respond.
3.  If the user asks about past analysis or "what did you recommend before",
    use the `load_memory` tool to search past conversations.

MEMORY CONTEXT RULES (applies when [MEMORY CONTEXT] block is present):
- [TODAY]: analysis was done today — you MUST return it directly.
- [STALE]: analysis is from a prior day — treat as background reference only, not as the current recommendation.

For general chat or non-stock queries, respond conversationally.\
"""


# Dynamically inject available sub-agents into the LLM system prompt
def _build_instruction() -> str:
    today = datetime.now(IST).date().isoformat()
    agent_list = _client.list_agents()
    if agent_list:
        preamble = _STATIC_PREAMBLE
        skill_lines = "\n".join(
            f"  - {a['name']}: {a['description']}"
            for a in agent_list
        )
        skill_lines += (
            "\n\nAgent responsibility boundaries:\n"
            "  - Financial RAG Agent owns ALL document and news retrieval\n"
            "  - Market Context Agent provides macro regime (rates, VIX, sector ETFs)\n"
            "    and peer landscape narrative — treat its output as 'context' for synthesis\n"
            "  - Quant Analysis Agent owns numeric risk, fundamentals, technicals, DCF,\n"
            "    Monte Carlo, peer comparison, and behavioral signals (options/insider/positioning)"
        )
    else:
        preamble = _STATIC_PREAMBLE_FALLBACK
        skill_lines = "  (none discovered yet — agents may still be starting up)"
    return (
        f"Today's date is {today}. Use this as the reference date for all analysis.\n\n"
        f"{preamble}\n\n"
        f"Available agents:\n{skill_lines}\n"
    )


# Async startup: discover sub-agents on boot, rebuild instruction once agents are known
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
    tools=[send_message, load_memory],
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
