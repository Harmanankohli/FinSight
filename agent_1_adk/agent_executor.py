import asyncio
import logging
import re
import uuid

from a2a.helpers import (
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from google.adk.runners import Runner
from google.genai import types
from langfuse import propagate_attributes

import os

from shared.config import EVAL_ENABLED
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_response as _eval_score_response
from shared.ticker_utils import extract_ticker

logger = logging.getLogger(__name__)

_SEMANTIC_CACHE_ENABLED = os.environ.get("SEMANTIC_CACHE_ENABLED", "false").lower() == "true"

_semantic_cache = None

def _get_semantic_cache():
    global _semantic_cache
    if _SEMANTIC_CACHE_ENABLED and _semantic_cache is None:
        try:
            from shared.semantic_cache import SemanticCache
            _semantic_cache = SemanticCache()
        except Exception as exc:
            logger.warning("SemanticCache init failed: %s", exc)
    return _semantic_cache

_NON_INVESTMENT_RE = re.compile(
    r"\b(weather|recipe|sports score|movie|song|joke|cook(?:ing)?|weather forecast|"
    r"horoscope|gaming|video game|celebrity|fashion|travel destination)\b",
    re.IGNORECASE,
)
_SIGNAL_RE = re.compile(r"\b(BUY|HOLD|SELL)\b")


class FinSightAgentExecutor(AgentExecutor):
    """Executor that runs the ADK orchestrator agent directly.

    The ADK agent (LlmAgent) has two tools:
    - ``list_remote_agents``: discover available sub-agents
    - ``send_message``: delegate tasks to sub-agents via A2A

    No pre-fetching. The LLM decides when to call tools.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        context_id = context.context_id
        user_id = _resolve_user_id(context)

        await self.ensure_session(user_id, context_id)

        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            new_text_message("Processing your request..."),
        )

        langfuse = get_langfuse_client()
        original_input = context.get_user_input()
        user_input = original_input

        ticker_hint = extract_ticker(user_input) or "unknown"

        # ── Input Guardrail: off-topic filter ────────────────────────────────
        if _NON_INVESTMENT_RE.search(original_input):
            task = context.current_task
            if not task:
                task = new_task_from_user_message(context.message)
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.update_status(
                TaskState.TASK_STATE_COMPLETED,
                new_text_message(
                    "I'm specialized in investment research. "
                    "Please ask about stocks, portfolios, or financial analysis."
                ),
                final=True,
            )
            return

        # ── Input Guardrail: invalid ticker pre-check ────────────────────────
        if ticker_hint != "unknown":
            _mcp = None
            try:
                from shared.mcp_client import MCPClient, MCPServerConfig
                from shared.config import MCP_SERVER_URL
                _mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)])
                await _mcp.connect_all()
                val_result = await _mcp.call_tool_by_name("validate_ticker", {"ticker": ticker_hint})
                import json as _json
                if hasattr(val_result, "content") and val_result.content:
                    raw = val_result.content[0].text if hasattr(val_result.content[0], "text") else str(val_result.content[0])
                    val_data = _json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(val_data, dict) and not val_data.get("valid", True):
                        task = context.current_task
                        if not task:
                            task = new_task_from_user_message(context.message)
                            await event_queue.enqueue_event(task)
                        updater = TaskUpdater(event_queue, task.id, task.context_id)
                        await updater.update_status(
                            TaskState.TASK_STATE_COMPLETED,
                            new_text_message(
                                f"Ticker '{ticker_hint}' was not found in SEC EDGAR. "
                                "Please verify the ticker symbol and try again."
                            ),
                            final=True,
                        )
                        return
            except Exception as _e:
                logger.debug("Ticker pre-check failed (non-fatal): %s", _e)
            finally:
                # Clean up temporary MCP connection
                if _mcp is not None:
                    try:
                        await _mcp.disconnect_all()
                        logger.debug("Temporary MCP connection closed")
                    except Exception as cleanup_err:
                        logger.debug("MCP cleanup error (non-critical): %s", cleanup_err)

        # ── Semantic cache check ─────────────────────────────────────────────
        sc = _get_semantic_cache()
        if sc is not None:
            cached_response = sc.get(original_input)
            if cached_response:
                task = context.current_task
                if not task:
                    task = new_task_from_user_message(context.message)
                    await event_queue.enqueue_event(task)
                updater = TaskUpdater(event_queue, task.id, task.context_id)
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    new_text_message("Processing your request..."),
                )
                await updater.update_status(
                    TaskState.TASK_STATE_COMPLETED,
                    new_text_message(cached_response, task.context_id, task.id),
                    final=True,
                )
                return

        memory_context = await self._build_memory_context(user_input, user_id)
        if memory_context:
            user_input = f"[MEMORY CONTEXT]\n{memory_context}\n[/MEMORY CONTEXT]\n\n{user_input}"

        content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_input)],
        )

        trace_id = str(uuid.uuid4())

        with langfuse.start_as_current_observation(
            as_type="span",
            name="finsight-query",
            trace_context={"trace_id": trace_id},
            input={"query": user_input},
            metadata={"ticker": ticker_hint, "context_id": context_id},
        ) as span:
            with propagate_attributes(session_id=context_id, user_id=user_id):
                collected_events: list = []
                final_event = None
                try:
                    async for event in self._runner.run_async(
                        user_id=user_id,
                        session_id=context_id,
                        new_message=content,
                    ):
                        collected_events.append(event)
                        if event.is_final_response():
                            final_event = event

                    if final_event:
                        await self._process_response(
                            final_event, updater, task, span, trace_id,
                            user_input, user_id, original_input,
                        )
                        logger.info("Collected %d events, calling _add_events_to_memory", len(collected_events))
                        await self._add_events_to_memory(
                            user_id, context_id, collected_events
                        )
                        # Also persist via memory service directly for load_memory
                        await self._persist_to_memory(user_id, context_id, collected_events)
                    else:
                        logger.warning("No final event received from runner")
                        await updater.update_status(
                            TaskState.TASK_STATE_FAILED,
                            new_text_message("No final response from agent"),
                        )
                except Exception:
                    logger.exception("Error during agent execution")
                    span.update(output={"error": "Agent execution failed"})
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        new_text_message(
                            "An exception occurred while performing the operation"
                        ),
                    )

    async def _process_response(
        self, event, updater: TaskUpdater, task, span=None, trace_id=None,
        user_input: str = "", user_id: str = "", original_input: str = "",
    ) -> None:
        if not (
            event.content
            and event.content.parts
            and event.content.parts[0].text
        ):
            if span:
                span.update(output={"error": "No text content"})
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message("[No text content]"),
            )
            return

        text = event.content.parts[0].text.strip()

        # ── Output Guardrail: empty / too-short response ─────────────────────
        if len(text) < 50:
            logger.warning("Orchestrator response too short (%d chars) — failing", len(text))
            if span:
                span.update(output={"error": "Response too short", "text": text})
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message("[Incomplete response from agent — please retry]"),
            )
            return

        # ── Output Guardrail: BUY/HOLD/SELL signal check ─────────────────────
        if not _SIGNAL_RE.search(text) and user_input and extract_ticker(user_input):
            logger.warning("Orchestrator response missing BUY/HOLD/SELL signal")
            if span:
                span.update(metadata={"missing_signal": True})

        if span:
            span.update(output={"response": text[:2000]})
            span.update(output={"synthesis": text[:2000]}, metadata={"completed": True})

        if "[TODAY" not in user_input:
            asyncio.create_task(
                self._store_memory(user_input, text, task.context_id, user_id)
            )
        if EVAL_ENABLED:
            asyncio.create_task(
                _eval_score_response(
                    original_input or user_input,
                    text,
                    trace_id,
                )
            )

        # Store in semantic cache for future identical/similar queries
        if original_input:
            sc = _get_semantic_cache()
            if sc is not None:
                try:
                    sc.set(original_input, text)
                except Exception:
                    pass

        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            new_text_message(text, task.context_id, task.id),
            final=True,
        )

    async def ensure_session(
        self, user_id: str, context_id: str
    ) -> None:
        session = await self._runner.session_service.get_session(
            app_name=self._runner.app_name,
            user_id=user_id,
            session_id=context_id,
        )
        if session is None:
            await self._runner.session_service.create_session(
                app_name=self._runner.app_name,
                user_id=user_id,
                session_id=context_id,
            )

    async def cancel(self, context, event_queue) -> None:
        raise NotImplementedError("Cancellation is not supported")

    async def _add_events_to_memory(
        self, user_id: str, context_id: str, events: list
    ) -> None:
        """Add the completed session to memory using the standard ADK pattern.

        ADK docs prescribe: run agent → get_session() → add_session_to_memory(session).
        This works with any MemoryService implementation (InMemoryMemoryService,
        SQLiteMemoryService, VertexAiMemoryBankService, etc.).
        """
        logger.info("_add_events_to_memory: %d events, user=%s, session=%s", len(events), user_id, context_id)
        if not events:
            logger.warning("No events to add to memory")
            return
        if not self._runner.memory_service:
            logger.warning("No memory_service available on runner")
            return
        try:
            # Standard ADK pattern: get the session (which now has events from DatabaseSessionService)
            # then add the full session to memory
            session = await self._runner.session_service.get_session(
                app_name=self._runner.app_name,
                user_id=user_id,
                session_id=context_id,
            )
            logger.info("Got session: %s, events: %s", session.id if session else "None", len(session.events) if session and session.events else 0)
            if session and session.events:
                logger.info("Session has %d events, adding to memory", len(session.events))
                await self._runner.memory_service.add_session_to_memory(session)
                logger.info("Successfully added session to memory")
            else:
                logger.warning(
                    "Session has no events (events=%s), falling back to add_events_to_memory",
                    "empty" if session else "none",
                )
                # Fallback: if session events are empty (e.g. InMemorySessionService doesn't persist)
                # use the events we collected during run_async
                await self._runner.memory_service.add_events_to_memory(
                    app_name=self._runner.app_name,
                    user_id=user_id,
                    events=events,
                    session_id=context_id,
                )
                logger.info("Successfully added %d events via fallback", len(events))
        except Exception:
            logger.error("Failed to add session to memory", exc_info=True)

    async def _build_memory_context(self, user_input: str, user_id: str) -> str:
        """Build compact memory context for prompt injection."""
        from datetime import date as _date
        from shared.memory import PortfolioStore, TickerMemory
        from shared.ticker_utils import extract_ticker

        ticker = extract_ticker(user_input)
        parts = []

        if ticker:
            tm = TickerMemory()
            latest = await tm.get_latest(ticker, user_id=user_id)
            if latest:
                context = await tm.format_context(ticker, max_tokens=300)
                if context:
                    # Prefer explicit analysis_date; fall back to created_at for old rows
                    analysis_date = latest.get("analysis_date")
                    if not analysis_date:
                        raw = latest["created_at"]
                        analysis_date = raw.split("T")[0] if "T" in raw else raw[:10]
                    today = _date.today().isoformat()
                    if analysis_date == today:
                        parts.append(f"[TODAY — analysis is current, you may return it directly without calling agents again] {context}")
                    else:
                        parts.append(
                            f"[STALE — analyzed on {analysis_date}, today is {today}. "
                            f"You MUST call ALL agents for a fresh analysis before responding. "
                            f"Do NOT return this as the current recommendation.] {context}"
                        )

        ps = PortfolioStore()
        holdings = await ps.get_holdings(user_id)
        if holdings:
            parts.append(f"Background — user's known holdings (do NOT include for portfolio correlation unless the user explicitly requests it in their current message): {', '.join(holdings)}")

        return "\n".join(parts)

    async def _store_memory(
        self, query: str, response_text: str, session_id: str, user_id: str
    ) -> None:
        """Parse response and store brief + portfolio + performance record."""
        from shared.memory import PerformanceTracker, PortfolioStore, TickerMemory
        from shared.models import QueryContext
        from shared.ticker_utils import extract_ticker

        ticker = extract_ticker(query) or "unknown"

        rec_match = re.search(r'\b(BUY|HOLD|SELL)\b', response_text, re.IGNORECASE)
        recommendation = rec_match.group(1).upper() if rec_match else "UNKNOWN"

        tm = TickerMemory()
        await tm.store_minimal(
            ticker=ticker,
            user_id=user_id,
            session_id=session_id,
            query=query,
            response_text=response_text,
            recommendation=recommendation,
        )

        pt = PerformanceTracker()
        await pt.record_recommendation(
            ticker=ticker,
            user_id=user_id,
            recommendation=recommendation,
            confidence=0.5,
        )

        try:
            ctx = QueryContext(
                ticker=ticker,
                user_query=query,
                user_risk_profile="",
                portfolio_holdings=[],
                investment_horizon="",
                session_id=session_id,
                timestamp=__import__("datetime").datetime.utcnow(),
            )
            ps = PortfolioStore()
            await ps.upsert_from_context(ctx)
        except Exception:
            logger.debug("Failed to update portfolio from query context", exc_info=True)

    async def _persist_to_memory(
        self, user_id: str, context_id: str, events: list
    ) -> None:
        """Persist events directly to SQLiteMemoryService for load_memory search.

        This bypasses the unreliable after_agent_callback and ensures
        conversation events are stored in memory_entries table.
        """
        if not events:
            return
        if not self._runner.memory_service:
            logger.warning("No memory_service on runner, skipping memory persistence")
            return
        try:
            await self._runner.memory_service.add_events_to_memory(
                app_name=self._runner.app_name,
                user_id=user_id,
                events=events,
                session_id=context_id,
            )
            logger.info("Persisted %d events to memory for load_memory", len(events))
        except Exception:
            logger.error("Failed to persist events to memory", exc_info=True)


def _resolve_user_id(context: RequestContext) -> str:
    if (
        context.call_context
        and context.call_context.user.is_authenticated
    ):
        return context.call_context.user.user_name
    return "a2a_user"
