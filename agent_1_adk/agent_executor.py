import asyncio
import json
import logging
import os
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

from agent_1_adk.agent import pop_agent_responses
from shared.guardrails import extract_ticker, is_off_topic
from shared.logging_config import logged, logged_sync
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_response as _eval_score_response
from shared.settings import AGENT_SEED_URLS, EVAL_ENABLED
from shared.trace_context import current_user_id

logger = logging.getLogger(__name__)

_SEMANTIC_CACHE_ENABLED = os.environ.get("SEMANTIC_CACHE_ENABLED", "false").lower() == "true"

_semantic_cache = None


@logged_sync(log_result=False)
def _get_semantic_cache():
    global _semantic_cache
    if _SEMANTIC_CACHE_ENABLED and _semantic_cache is None:
        try:
            from shared.semantic_cache import SemanticCache

            _semantic_cache = SemanticCache()
        except Exception as exc:
            logger.warning("SemanticCache init failed: %s", exc)
    return _semantic_cache


_SIGNAL_RE = re.compile(r"\b(BUY|HOLD|SELL)\b")


async def _release_sub_agent_evals() -> None:
    """POST /release-evals to each sub-agent so they fire deferred evals."""
    import httpx

    urls = [u.strip().rstrip("/") for u in AGENT_SEED_URLS.split(",") if u.strip()]
    async with httpx.AsyncClient(timeout=5) as client:
        for base in urls:
            try:
                await client.post(f"{base}/release-evals")
            except Exception:
                logger.debug("release-evals failed for %s (non-fatal)", base)


class FinSightAgentExecutor(AgentExecutor):
    """Executor that runs the ADK orchestrator agent directly.

    The ADK agent (LlmAgent) has two tools:
    - ``list_remote_agents``: discover available sub-agents
    - ``send_message``: delegate tasks to sub-agents via A2A

    No pre-fetching. The LLM decides when to call tools.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._task: asyncio.Task | None = None

    @logged()
    async def _get_today_cached_text(self, ticker: str, user_id: str) -> str | None:
        """Return today's cached analysis text, or None if not available."""
        import json
        from datetime import datetime

        from shared.memory import TickerMemory
        from shared.settings import IST

        tm = TickerMemory()
        # Query without user_id filter — user_id varies across endpoints (a2a_user / user / eval_user)  # noqa: E501
        latest = await tm.get_latest(ticker, user_id=None)
        if not latest:
            logger.debug("Memory cache miss for %s (no record)", ticker)
            return None

        analysis_date = latest.get("analysis_date") or latest["created_at"][:10]
        today = datetime.now(IST).date().isoformat()
        if analysis_date != today:
            logger.debug(
                "Memory cache miss for %s (stale: %s vs today %s)", ticker, analysis_date, today
            )
            return None

        rec = latest.get("recommendation", "UNKNOWN")
        conf = latest.get("confidence", 0.5)

        try:
            data = json.loads(latest.get("brief_json", "{}"))
            response_text = data.get("response_text", "")
        except Exception:
            response_text = ""

        logger.info(
            "Memory cache HIT for %s (rec=%s, conf=%.2f, text_len=%d)",
            ticker,
            rec,
            conf,
            len(response_text),
        )

        if response_text:
            return f"**{ticker} — {rec}** (confidence: {conf:.0%})\n\n{response_text}"
        if rec != "UNKNOWN":
            return f"**{ticker} — {rec}** (confidence: {conf:.0%})"
        return None

    @logged(log_args=False, log_result=False)
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._task = asyncio.current_task()
        context_id = context.context_id
        user_id = _resolve_user_id(context)
        current_user_id.set(user_id)

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

        # Skip LLM inference for non-investment queries — saves cost and latency
        # ── Input Guardrail: off-topic filter ────────────────────────────────
        if is_off_topic(original_input):
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

        # Fail fast on invalid tickers to avoid wasted agent calls downstream
        # ── Input Guardrail: invalid ticker pre-check ────────────────────────
        if ticker_hint != "unknown":
            try:
                from shared.ticker_utils import validate_ticker as _validate_ticker

                _valid, _, _err = await _validate_ticker(ticker_hint)
                if not _valid:
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

        # Short-circuit identical queries via semantic similarity cache
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
                    new_text_message(cached_response),
                    final=True,
                )
                return

        # Hard short-circuit: today's brief exists — return it directly, skip LLM + sub-agents
        if ticker_hint != "unknown":
            cached = await self._get_today_cached_text(ticker_hint, user_id)
            if cached:
                await updater.update_status(
                    TaskState.TASK_STATE_COMPLETED,
                    new_text_message(cached),
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
                # Main execute loop: run the ADK agent and collect all streaming events
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
                            final_event,
                            updater,
                            task,
                            span,
                            trace_id,
                            user_input,
                            user_id,
                            original_input,
                        )
                        logger.info(
                            "Collected %d events, calling _add_events_to_memory",
                            len(collected_events),
                        )
                        await self._add_events_to_memory(user_id, context_id, collected_events)
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
                        new_text_message("An exception occurred while performing the operation"),
                    )

    # Output guardrails: reject too-short responses, warn when BUY/HOLD/SELL signal is missing
    @logged(log_result=False)
    async def _process_response(
        self,
        event,
        updater: TaskUpdater,
        task,
        span=None,
        trace_id=None,
        user_input: str = "",
        user_id: str = "",
        original_input: str = "",
    ) -> None:
        if not (event.content and event.content.parts and event.content.parts[0].text):
            if span:
                span.update(output={"error": "No text content"})
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message("[No text content]"),
            )
            return

        text = event.content.parts[0].text.strip()

        # Reject responses shorter than 50 chars — likely a hallucination or refusal
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

        # Warn when a stock query response is missing a BUY/HOLD/SELL verdict
        # ── Output Guardrail: BUY/HOLD/SELL signal check ─────────────────────
        if not _SIGNAL_RE.search(text) and user_input and extract_ticker(user_input):
            logger.warning("Orchestrator response missing BUY/HOLD/SELL signal")
            if span:
                span.update(metadata={"missing_signal": True})

        if span:
            span.update(output={"response": text[:2000]})
            span.update(output={"synthesis": text[:2000]}, metadata={"completed": True})

        if "[TODAY" not in user_input:
            asyncio.create_task(self._store_memory(user_input, text, task.context_id, user_id))
        if EVAL_ENABLED:
            asyncio.create_task(
                _eval_score_response(
                    original_input or user_input,
                    text,
                    trace_id,
                )
            )
            asyncio.create_task(_release_sub_agent_evals())

        # Store in semantic cache for future identical/similar queries
        if original_input:
            sc = _get_semantic_cache()
            if sc is not None:
                try:
                    sc.set(original_input, text)
                except Exception:
                    logger.debug("Semantic cache write failed", exc_info=True)

        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            new_text_message(text),
            final=True,
        )

    @logged()
    async def ensure_session(self, user_id: str, context_id: str) -> None:
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
        if self._task and not self._task.done():
            self._task.cancel()

    @logged(log_result=False)
    async def _add_events_to_memory(self, user_id: str, context_id: str, events: list) -> None:
        """Add the completed session to memory using the standard ADK pattern.

        ADK docs prescribe: run agent → get_session() → add_session_to_memory(session).
        This works with any MemoryService implementation (InMemoryMemoryService,
        SQLiteMemoryService, VertexAiMemoryBankService, etc.).
        """
        logger.info(
            "_add_events_to_memory: %d events, user=%s, session=%s",
            len(events),
            user_id,
            context_id,
        )
        if not events:
            logger.warning("No events to add to memory")
            return
        if not self._runner.memory_service:
            logger.warning("No memory_service available on runner")
            return
        try:
            # Standard ADK pattern: get the session (which now has events from DatabaseSessionService)  # noqa: E501
            # then add the full session to memory
            session = await self._runner.session_service.get_session(
                app_name=self._runner.app_name,
                user_id=user_id,
                session_id=context_id,
            )
            logger.info(
                "Got session: %s, events: %s",
                session.id if session else "None",
                len(session.events) if session and session.events else 0,
            )
            if session and session.events:
                logger.info("Session has %d events, adding to memory", len(session.events))
                await self._runner.memory_service.add_session_to_memory(session)
                logger.info("Successfully added session to memory")
            else:
                logger.warning(
                    "Session has no events (events=%s), falling back to add_events_to_memory",
                    "empty" if session else "none",
                )
                # Fallback: if session events are empty (e.g. InMemorySessionService doesn't persist)  # noqa: E501
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

    # Label past analysis TODAY (return directly) vs STALE (must re-run agents)
    @logged(log_result=False)
    async def _build_memory_context(self, user_input: str, user_id: str) -> str:
        """Build compact memory context for prompt injection."""
        from datetime import datetime

        from shared.memory import PortfolioStore, TickerMemory
        from shared.settings import IST
        from shared.ticker_utils import extract_ticker

        ticker = extract_ticker(user_input)
        parts = []

        if ticker:
            tm = TickerMemory()
            latest = await tm.get_latest(ticker, user_id=None)
            if latest:
                context = await tm.format_context(ticker, max_tokens=300)
                if context:
                    # Prefer explicit analysis_date; fall back to created_at for old rows
                    analysis_date = latest.get("analysis_date")
                    if not analysis_date:
                        raw = latest["created_at"]
                        analysis_date = raw.split("T")[0] if "T" in raw else raw[:10]
                    today = datetime.now(IST).date().isoformat()
                    if analysis_date == today:
                        parts.append(
                            f"[TODAY — analysis is current, you may return it directly without calling agents again] {context}"  # noqa: E501
                        )
                    else:
                        parts.append(
                            f"[STALE — analyzed on {analysis_date}, today is {today}. "
                            f"You MUST call ALL agents for a fresh analysis before responding. "
                            f"Do NOT return this as the current recommendation.] {context}"
                        )

        ps = PortfolioStore()
        holdings = await ps.get_holdings(user_id)
        if holdings:
            parts.append(
                f"Background — user's known holdings (do NOT include for portfolio correlation unless the user explicitly requests it in their current message): {', '.join(holdings)}"  # noqa: E501
            )

        return "\n".join(parts)

    async def _store_memory(
        self, query: str, response_text: str, session_id: str, user_id: str
    ) -> None:
        """Parse response and store brief + portfolio + performance record."""
        from datetime import datetime

        from shared.memory import PerformanceTracker, PortfolioStore, TickerMemory
        from shared.models import QueryContext
        from shared.settings import IST
        from shared.ticker_utils import extract_ticker

        ticker = extract_ticker(query) or "unknown"

        # Capture structured agent outputs before dedup check
        agent_outputs = pop_agent_responses(session_id)
        extra = {}
        for agent_name, data in agent_outputs.items():
            name_lower = agent_name.lower()
            if "rag" in name_lower:
                if "context_texts" in data:
                    data["context_texts"] = [t[:500] for t in data["context_texts"][:3]]
                extra["rag_response"] = data
            elif "quant" in name_lower:
                extra["quant_response"] = data
            elif "market" in name_lower or "sentiment" in name_lower:
                extra["sentiment_response"] = data

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
                    logger.debug("Could not parse brief_json for %s", ticker, exc_info=True)
                if len(response_text) > len(stored):
                    await tm.update_response_text(existing["id"], response_text)
                    logger.info(
                        "Updated existing brief %s with longer text (%d > %d)",
                        existing["id"],
                        len(response_text),
                        len(stored),
                    )
                else:
                    logger.debug("Skip _store_memory — brief already stored today for %s", ticker)
                return

        rec_match = re.search(r"\b(BUY|HOLD|SELL)\b", response_text, re.IGNORECASE)
        recommendation = rec_match.group(1).upper() if rec_match else "UNKNOWN"

        confidence = 0.5
        conf_match = re.search(
            r"(?:confidence|conf)(?:\s+score)?[:\s]*(\d+(?:\.\d+)?)\s*%?"
            r"|(\d+(?:\.\d+)?)\s*%\s*(?:confidence|conf)",
            response_text,
            re.IGNORECASE,
        )
        if conf_match:
            raw = float(conf_match.group(1) or conf_match.group(2))
            confidence = raw / 100.0 if raw > 1.0 else raw

        await tm.store_minimal(
            ticker=ticker,
            user_id=user_id,
            session_id=session_id,
            query=query,
            response_text=response_text,
            recommendation=recommendation,
            confidence=round(confidence, 2),
            extra_data=extra if extra else None,
        )

        pt = PerformanceTracker()
        await pt.record_recommendation(
            ticker=ticker,
            user_id=user_id,
            recommendation=recommendation,
            confidence=round(confidence, 2),
        )

        try:
            ctx = QueryContext(
                ticker=ticker,
                user_query=query,
                user_risk_profile="",
                portfolio_holdings=[],
                investment_horizon="",
                session_id=session_id,
                timestamp=datetime.now(IST),
            )
            ps = PortfolioStore()
            await ps.upsert_from_context(ctx)
        except Exception:
            logger.debug("Failed to update portfolio from query context", exc_info=True)

    # Fallback: persist directly to SQLiteMemoryService for load_memory tool support
    async def _persist_to_memory(self, user_id: str, context_id: str, events: list) -> None:
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
    if context.call_context and context.call_context.user.is_authenticated:
        return context.call_context.user.user_name
    return "a2a_user"
