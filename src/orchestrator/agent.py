"""Orchestrator agent definition: ADK LlmAgent, sub-agent delegation tools, and memory persistence.

Provides the root_agent (LlmAgent) that coordinates specialized investment sub-agents
via A2A protocol, along with tool functions for task delegation, memory search, and
investment brief persistence.
"""
# ruff: noqa: E402, E501
import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from shared.settings import ADK_MODEL, IST, get_settings

logger = logging.getLogger(__name__)

from .sub_agent_client import SubAgentClient

_s = get_settings()
_client = SubAgentClient(bearer_token=_s.service_auth_token or None)

# Capture raw agent responses keyed by session_id for structured storage
_agent_responses: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_lock = threading.Lock()


# ADK tool function: delegates tasks to sub-agents via A2A
def _trim_for_llm(agent_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return a condensed copy of agent output for the orchestrator LLM.

    Keeps fields needed by the reviewer tools and key signals for synthesis.
    Full output is preserved in _agent_responses for storage/reports.
    """
    lower = agent_name.lower()

    if "quant" in lower:
        metrics = data.get("metrics") or {}
        dcf = data.get("dcf_valuation") or {}
        return {
            "ticker": data.get("ticker"),
            "recommendation": data.get("recommendation"),
            "reasoning": (data.get("reasoning") or "")[:300],
            "metrics": {
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "var_95_daily": metrics.get("var_95_daily"),
                "quant_confidence": metrics.get("quant_confidence"),
                "quant_signal": metrics.get("quant_signal"),
            },
            "dcf_valuation": {
                "intrinsic_value": dcf.get("intrinsic_value"),
                "current_price": dcf.get("current_price"),
                "upside_pct": dcf.get("upside_pct"),
            }
            if dcf
            else None,
        }

    if "rag" in lower:
        return {
            "ticker": data.get("ticker"),
            "summary": (data.get("summary") or "")[:500],
            "confidence_score": data.get("confidence_score"),
            "sources": data.get("sources", [])[:5],
        }

    if "market" in lower:
        return {
            "overall_signal": data.get("overall_signal"),
            "confidence_score": data.get("confidence_score"),
            "narrative": (data.get("narrative") or "")[:300],
            "key_tailwinds": data.get("key_tailwinds", [])[:3],
            "key_headwinds": data.get("key_headwinds", [])[:3],
        }

    if "analytics" in lower:
        return {
            "ticker": data.get("ticker"),
            "trend_analysis": data.get("trend_analysis"),
            "anomalies": {
                "severity": (data.get("anomalies") or {}).get("severity"),
                "anomaly_count": (data.get("anomalies") or {}).get("anomaly_count"),
            }
            if data.get("anomalies")
            else None,
            "analytics_confidence": data.get("analytics_confidence"),
            "analytics_signal": data.get("analytics_signal"),
            "forecast": {
                "method": (data.get("forecast") or {}).get("method"),
                "forecast_dates": (data.get("forecast") or {}).get("forecast_dates", [])[:3],
            }
            if data.get("forecast")
            else None,
        }

    return data


_AGENT_TASK_TEMPLATES: dict[str, str] = {
    "financial rag agent": "Analyze {ticker} SEC filings, earnings reports, and recent financial news.",
    "quant analysis agent": "Perform quantitative analysis on {ticker} including DCF valuation, technical indicators, peer comparison, and risk metrics.",
    "market context agent": "Analyze {ticker} market context including macro regime, competitive landscape, and industry trends.",
    "analytics agent": "Run statistical trend analysis, forecasting, and anomaly detection for {ticker}.",
    "reviewer agent": '{{"ticker": "{ticker}"}}',
}


class SendMessageInput(BaseModel):
    """Input for delegating a task to a specialized investment agent."""

    agent_name: str = Field(
        description="The EXACT name of the agent from the Available agents list. "
        "Valid names: Financial RAG Agent, Quant Analysis Agent, "
        "Market Context Agent, Analytics Agent, Reviewer Agent.",
    )
    ticker: str = Field(
        description="Stock ticker symbol in ALL CAPS (e.g. AAPL, NVDA, MA, MSFT). "
        "Must be the same ticker for every agent call.",
        pattern=r"^[A-Z]{1,5}$",
    )


async def send_message(args: SendMessageInput, tool_context: ToolContext) -> str:
    """Delegate a task to a specialized remote investment agent.

    Call this for EACH agent listed under "Available agents". Use the EXACT
    agent name from that list. Pass the stock ticker in ALL CAPS.
    """
    agent_name = args.agent_name
    ticker = args.ticker.strip().upper()

    if not _client.list_agents():
        return json.dumps(
            {
                "error": "No agents are currently available. They may still be "
                "starting up. Do not call this tool — answer based on "
                "your own knowledge instead."
            }
        )
    resolved = _client.resolve_agent_name(agent_name)
    if resolved is None:
        valid = [a["name"] for a in _client.list_agents()]
        return json.dumps(
            {
                "error": f"Unknown agent '{agent_name}'. Valid agents are: {valid}. "
                "Use one of these exact names and retry."
            }
        )

    template = _AGENT_TASK_TEMPLATES.get(resolved.lower(), "Analyze {ticker}.")
    task = template.format(ticker=ticker)

    # Capture session_id from tool context for storing outputs
    session_id = tool_context.session.id if tool_context and tool_context.session else None

    # Inject session_id AND inline agent outputs into reviewer query
    if session_id and "reviewer" in resolved.lower():
        try:
            payload = json.loads(task)
        except (json.JSONDecodeError, TypeError):
            payload = {"ticker": ticker}
        payload["session_id"] = session_id
        entry = _agent_responses.get(session_id)
        if entry:
            from shared.memory.agent_output_store import _normalize_agent_key

            payload["agent_outputs"] = {_normalize_agent_key(k): v for k, v in entry[1].items()}
        task = json.dumps(payload)

    result = await _client.send_message(resolved, task)
    logger.info(
        "send_message capture: agent=%s session_id=%s has_result=%s",
        resolved,
        session_id,
        bool(result),
    )
    parsed = None
    if session_id and result:
        with _session_locks_lock:
            if session_id not in _session_locks:
                _session_locks[session_id] = asyncio.Lock()
        async with _session_locks[session_id]:
            ts, bucket = _agent_responses.get(session_id, (time.monotonic(), {}))
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                parsed = {"_raw_text": result[:5000]}
            bucket[resolved] = parsed
            _agent_responses[session_id] = (ts, bucket)

        # Persist full output to shared SQLite store for reviewer to fetch directly
        if parsed:
            from shared.memory.agent_output_store import store_agent_output

            await store_agent_output(session_id, resolved, parsed)

        logger.info(
            "send_message captured: session=%s agents_so_far=%s is_json=%s",
            session_id,
            list(bucket.keys()),
            "_raw_text" not in parsed,
        )

    # Return condensed output to the LLM to reduce token processing time
    if parsed and not parsed.get("_raw_text") and not parsed.get("error"):
        return json.dumps(_trim_for_llm(resolved, parsed))

    return str(result)


def pop_agent_responses(session_id: str) -> dict[str, dict[str, Any]]:
    """Pop and return captured agent responses for a session. Sweeps stale entries.

    Falls back to the most recent entry if exact session_id match fails,
    since ADK's internal session ID may differ from the A2A context_id.
    """
    now = time.monotonic()
    cutoff = now - 300
    stale = [k for k, (ts, _) in _agent_responses.items() if ts < cutoff]
    for k in stale:
        _agent_responses.pop(k, None)
    if len(_agent_responses) > 100:
        overflow = sorted(_agent_responses.items(), key=lambda kv: kv[1][0])[:-50]
        for k, _ in overflow:
            _agent_responses.pop(k, None)
    logger.info(
        "pop_agent_responses: session=%s stored_keys=%s",
        session_id,
        list(_agent_responses.keys()),
    )
    with _session_locks_lock:
        _session_locks.pop(session_id, None)
    entry = _agent_responses.pop(session_id, None)
    result = entry[1] if entry else {}
    logger.info(
        "pop_agent_responses: session=%s found=%s agents=%s",
        session_id,
        entry is not None,
        list(result.keys()),
    )
    return result


def _synthesis_text_from_context(tool_context: ToolContext) -> str:
    """Extract the longest LLM-generated text from the current turn in session events."""
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
    for event in events[last_user_idx + 1 :]:
        author = getattr(event, "author", None)
        if not author or author == "user":
            continue
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        text = "".join(p.text for p in content.parts if getattr(p, "text", None))
        if len(text) > len(best):
            best = text
    return best


# Dedup: skip if brief for this ticker was already saved today
async def save_brief(
    ticker: str,
    recommendation: str,
    confidence: float,
    rationale: str = "",
    tool_context: ToolContext | None = None,
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
    from shared.memory.performance_tracker import PerformanceTracker
    from shared.memory.ticker_memory import TickerMemory

    session_id = tool_context.session.id if tool_context and tool_context.session else "unknown"
    user_id = tool_context.user_id if tool_context else "default_user"

    tm = TickerMemory()

    # Peek at agent responses for this session (don't pop — _store_memory handles cleanup)
    agent_extra = {}
    entry = _agent_responses.get(session_id)
    if entry:
        for agent_name, data in entry[1].items():
            name_lower = agent_name.lower()
            if "rag" in name_lower:
                truncated = dict(data)
                if "context_texts" in truncated:
                    truncated["context_texts"] = [t[:500] for t in truncated["context_texts"][:3]]
                agent_extra["rag_response"] = truncated
            elif "quant" in name_lower:
                agent_extra["quant_response"] = data
            elif "market" in name_lower or "sentiment" in name_lower:
                agent_extra["sentiment_response"] = data
            elif "analytics" in name_lower:
                agent_extra["analytics_response"] = data
            elif "reviewer" in name_lower:
                agent_extra["reviewer_response"] = data

    # Skip if a brief for this ticker was already saved today (dedup)
    existing = await tm.get_latest(ticker, user_id=user_id)
    if existing:
        ad = existing.get("analysis_date") or existing["created_at"][:10]
        if ad == datetime.now(IST).date().isoformat():
            # If existing brief lacks agent data, patch it in
            if agent_extra:
                try:
                    bj = json.loads(existing.get("brief_json", "{}"))
                    missing_agent = not any(
                        k in bj
                        for k in (
                            "quant_response",
                            "rag_response",
                            "sentiment_response",
                            "analytics_response",
                            "reviewer_response",
                        )
                    )
                    if missing_agent:
                        bj.update(agent_extra)
                        await tm.update_brief_json(existing["id"], json.dumps(bj))
                except Exception:
                    logger.debug(
                        "Could not patch agent data into existing brief for %s",
                        ticker,
                        exc_info=True,
                    )
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
        extra_data=agent_extra if agent_extra else None,
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


async def load_memory(query: str, tool_context: ToolContext | None = None) -> str:
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
        from shared.memory.performance_tracker import PerformanceTracker as _PT

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

PHASE 1 — Parallel Analysis:
2.  YOUR VERY FIRST ACTION must be to call `send_message` for EVERY Phase 1
    agent listed under "Available agents" below (Financial RAG Agent, Quant
    Analysis Agent, Market Context Agent, Analytics Agent). Do NOT call
    `load_memory` first. Do NOT call any other tool first. You MUST emit ALL
    Phase 1 `send_message` calls in a SINGLE assistant response so they
    execute in PARALLEL. Use their EXACT names — never invent agent names.
3.  Pass the SAME ticker symbol in ALL CAPS (e.g. "MA") to every agent.
    The task description is generated automatically — you only provide
    `agent_name` and `ticker`.

PHASE 2 — Review:
4.  After ALL Phase 1 agents have responded (you will receive their results
    together in the next turn), call `send_message` for the Reviewer Agent
    with the same ticker.

PHASE 3 — Synthesis:
5.  After the Reviewer Agent responds, synthesize ALL findings including the
    reviewer's cross-validation into a BUY/HOLD/SELL recommendation with
    supporting evidence. Include a confidence score (0.0–1.0) in your response.
    Your analysis is automatically saved after you respond — no manual save
    step needed.

TOOL RULES:
- `send_message`: Use this for ALL stock analysis requests. ALWAYS call it first.
  Parameters: agent_name (exact name from list), ticker (ALL CAPS symbol).
- `load_memory`: ONLY use this when the user explicitly asks about past
  recommendations (e.g. "what did you recommend before", "show history").
  NEVER call load_memory when the user asks to analyze a stock.

MEMORY CONTEXT RULES (applies when [MEMORY CONTEXT] block is present):
- [TODAY]: analysis was done today — you MUST return it directly without calling agents again.
- [STALE]: analysis is from a prior day — you MUST call ALL agents for a fresh analysis.
  Treat stale data as background reference only. Do NOT return it as the current recommendation.

EXAMPLES:
  User: "Analyze Mastercard" → send_message(agent_name="Financial RAG Agent", ticker="MA")
  User: "What about Apple?" → send_message(agent_name="Quant Analysis Agent", ticker="AAPL")

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
- [STALE]: analysis is from a prior day — treat as background reference only, not as the current recommendation.  # noqa: E501

For general chat or non-stock queries, respond conversationally.\
"""


# Dynamically inject available sub-agents into the LLM system prompt
def _build_instruction(session_id: str | None = None) -> str:
    """Build the dynamic system prompt with today's date and available sub-agents."""
    today = datetime.now(IST).date().isoformat()
    agent_list = _client.list_agents()
    if agent_list:
        preamble = _STATIC_PREAMBLE
        skill_lines = "\n".join(f"  - {a['name']}: {a['description']}" for a in agent_list)
        skill_lines += (
            "\n\nAgent responsibility boundaries:\n"
            "  - Financial RAG Agent owns ALL document and news retrieval\n"
            "  - Market Context Agent provides macro regime (rates, VIX, sector ETFs),\n"
            "    peer landscape narrative, and web search context for macro/sector commentary\n"
            "    — treat its output as 'context' for synthesis\n"
            "  - Quant Analysis Agent owns numeric risk, fundamentals, technicals, DCF,\n"
            "    Monte Carlo, peer comparison, behavioral signals (options/insider/positioning),\n"
            "    and web context for analyst opinions and news catalysts\n"
            "  - Analytics Agent (PHASE 1) owns trend detection, forecasting, chart data,\n"
            "    statistical analysis, and anomaly detection\n"
            "  - Reviewer Agent (PHASE 2 ONLY) cross-validates all agent outputs, checks\n"
            "    for contradictions, and produces calibrated meta-confidence"
        )
    else:
        preamble = _STATIC_PREAMBLE_FALLBACK
        skill_lines = "  (none discovered yet — agents may still be starting up)"
    session_line = f"\n\nYour current session_id is: {session_id}" if session_id else ""
    return (
        f"Today's date is {today}. Use this as the reference date for all analysis.\n\n"
        f"{preamble}\n\n"
        f"Available agents:\n{skill_lines}{session_line}\n"
    )


# Async startup: discover sub-agents on boot
async def discover_background() -> None:
    """Discover available sub-agents by polling the A2A registry on startup."""
    await _client.discover()
    agent_list = _client.list_agents()
    logger.info(
        "Discovered %d agents: %s",
        len(agent_list),
        [a["name"] for a in agent_list],
    )


def _instruction_provider(ctx: Any = None) -> str:
    """Provide the dynamic system prompt, extracting session_id from the ADK context."""
    session_id = None
    if ctx and hasattr(ctx, "session") and ctx.session and hasattr(ctx.session, "id"):
        session_id = ctx.session.id
    return _build_instruction(session_id=session_id)


root_agent = LlmAgent(
    name="orchestrator",
    model=ADK_MODEL,
    description=(
        "Coordinates specialized investment agents into a comprehensive "
        "Investment Brief via A2A protocol"
    ),
    instruction=_instruction_provider,
    tools=[send_message, load_memory],
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.0,
    ),
)
root_agent._sub_agent_client = _client  # type: ignore[attr-defined]


async def start_background_discovery() -> None:
    """Discover sub-agents at startup, called from the Starlette lifespan to avoid import-time side effects."""
    await discover_background()
