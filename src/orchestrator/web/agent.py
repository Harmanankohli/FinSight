# ruff: noqa: E402
"""ADK callback layer for `adk web` mode.

Handles post-turn memory persistence and orchestrator-level RAGAS eval.
Bypassed entirely when running through FinSightAgentExecutor."""

import asyncio
import logging
import re
from pathlib import Path

from shared.bootstrap import bootstrap

bootstrap("adk_web")

from google.genai import types

from orchestrator.agent import root_agent
from shared.runtime_eval import score_response as _eval_score_response
from shared.settings import AGENT_SEED_URLS, EVAL_ENABLED

__all__ = ["root_agent"]

logger = logging.getLogger(__name__)


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


async def _memory_cache_callback(callback_context) -> types.Content | None:
    """Before-agent callback: short-circuit with today's cached brief if available."""
    import json
    from datetime import datetime

    from shared.memory import TickerMemory
    from shared.settings import IST
    from shared.ticker_utils import extract_ticker

    def _log(msg):
        logger.info(msg)
        with open(_LOG_FILE, "a") as f:
            f.write(f"[cache] {msg}\n")

    _log("before_agent_callback fired")

    session = callback_context.session
    if not session or not session.events:
        _log("no session or no events")
        return None

    user_text = ""
    for event in reversed(session.events):
        if getattr(event, "author", None) == "user":
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                text = "".join(p.text for p in content.parts if getattr(p, "text", None))
                if text:
                    user_text = text
                    break

    _log(f"user_text={user_text!r:.80}")

    if not user_text:
        return None

    ticker = extract_ticker(user_text)
    _log(f"ticker={ticker!r}")
    if not ticker:
        return None

    tm = TickerMemory()
    latest = await tm.get_latest(ticker, user_id=None)
    _log(f"latest={latest is not None}")

    # Regex extracted a company-name token (e.g. "VISA") that isn't the
    # canonical ticker ("V"). Fall back to MCP company-name resolution
    # so the cached brief is still returned instead of re-running agents.
    if not latest:
        try:
            from shared.ticker_utils import resolve_ticker

            resolved, _company = await resolve_ticker(user_text)
            if resolved and resolved.upper() != ticker.upper():
                _log(f"regex={ticker!r} resolved={resolved!r}")
                ticker = resolved.upper()
                latest = await tm.get_latest(ticker, user_id=None)
                _log(f"latest_after_resolve={latest is not None}")
        except Exception as e:
            _log(f"resolve_ticker failed: {e}")

    if not latest:
        return None

    analysis_date = latest.get("analysis_date") or latest["created_at"][:10]
    today = datetime.now(IST).date().isoformat()
    _log(f"analysis_date={analysis_date!r} today={today!r} match={analysis_date == today}")
    if analysis_date != today:
        return None

    rec = latest.get("recommendation", "UNKNOWN")
    conf = latest.get("confidence", 0.5)

    try:
        data = json.loads(latest.get("brief_json", "{}"))
        response_text = data.get("response_text", "")
    except Exception:
        response_text = ""

    _log(f"rec={rec!r} conf={conf} rt_len={len(response_text)}")

    if not response_text and rec == "UNKNOWN":
        return None

    cached = f"**{ticker} — {rec}** (confidence: {conf:.0%})"
    if response_text:
        cached += f"\n\n{response_text}"

    _log(f"CACHE HIT — returning cached response for {ticker}")
    return types.Content(role="model", parts=[types.Part.from_text(text=cached)])


_LOGS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "logs"
_LOGS_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOGS_DIR / "memory_callback.log"

# Module load marker — proves the new code was loaded
with open(_LOG_FILE, "a") as _f:
    from datetime import datetime as _dt

    _f.write(f"\n=== MODULE LOADED at {_dt.now().isoformat()} ===\n")


# Pulls the user query + the synthesized analysis text from session events.
# Uses the LONGEST LLM text from the current turn (events after the last user
# message). This runs in after_agent_callback so the full synthesis is available.
def _extract_query_and_response(events) -> tuple[str, str]:
    """Pull the last user query and the longest LLM response from the current turn."""
    # Find the last user message index so we scope to the current turn only.
    last_user_idx = -1
    for i in range(len(events) - 1, -1, -1):
        if getattr(events[i], "author", None) == "user":
            last_user_idx = i
            break

    user_query = ""
    if last_user_idx >= 0:
        content = getattr(events[last_user_idx], "content", None)
        if content and getattr(content, "parts", None):
            user_query = "".join(p.text for p in content.parts if getattr(p, "text", None))

    # Take the longest LLM text produced after the last user message.
    best_text = ""
    for event in events[last_user_idx + 1 :]:
        author = getattr(event, "author", None)
        if not author or author == "user":
            continue
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        text = "".join(p.text for p in content.parts if getattr(p, "text", None))
        if len(text) > len(best_text):
            best_text = text

    return user_query, best_text


def _is_analysis_turn(events) -> bool:
    """True if the current turn produced a fresh analysis.

    Detects analysis by checking if send_message was called (normal path),
    or if the response contains a BUY/HOLD/SELL signal (fallback/no-agents path).
    A turn that only calls load_memory returns False.
    """
    last_user_idx = -1
    for i in range(len(events) - 1, -1, -1):
        if getattr(events[i], "author", None) == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return False

    for event in events[last_user_idx + 1 :]:
        try:
            for fn_call in event.get_function_calls():
                if fn_call.name == "send_message":
                    return True
        except Exception:
            continue

    # Fallback: check if the LLM response contains a BUY/HOLD/SELL signal
    # (covers the no-agents-available path where LLM analyzes on its own)
    for event in events[last_user_idx + 1 :]:
        author = getattr(event, "author", None)
        if not author or author == "user":
            continue
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        text = "".join(p.text for p in content.parts if getattr(p, "text", None))
        if re.search(r"\b(BUY|HOLD|SELL)\b", text):
            return True

    return False


_REC_PATTERN = re.compile(r"\b(BUY|HOLD|SELL)\b", re.IGNORECASE)
_CONF_PATTERN = re.compile(
    r"(?:confidence|conf)(?:\s+score)?[:\s]*(\d+(?:\.\d+)?)\s*%?"
    r"|(\d+(?:\.\d+)?)\s*%\s*(?:confidence|conf)",
    re.IGNORECASE,
)


def _extract_recommendation_from_text(text: str) -> tuple[str, float]:
    """Best-effort extraction of recommendation + confidence from LLM synthesis text."""
    rec = "UNKNOWN"
    conf = 0.5
    m = _REC_PATTERN.search(text)
    if m:
        rec = m.group(1).upper()
    cm = _CONF_PATTERN.search(text)
    if cm:
        raw = cm.group(1) or cm.group(2)
        val = float(raw)
        conf = val / 100.0 if val > 1.0 else val
    return rec, round(conf, 2)


async def _auto_save_brief(session, user_query: str, response_text: str) -> None:
    """Auto-save the investment brief after the turn completes.

    Extracts BUY/HOLD/SELL and confidence from the full response text,
    then persists via TickerMemory + PerformanceTracker.
    """
    from datetime import datetime

    from shared.memory import PerformanceTracker, TickerMemory
    from shared.settings import IST
    from shared.ticker_utils import extract_ticker

    ticker = extract_ticker(user_query)
    if not ticker:
        logger.warning("auto_save_brief: could not extract ticker from query")
        return

    user_id = session.user_id or "default_user"
    tm = TickerMemory()

    existing = await tm.get_latest(ticker, user_id=None)
    if existing:
        ad = existing.get("analysis_date") or existing["created_at"][:10]
        if ad == datetime.now(IST).date().isoformat():
            # Update with longer text if available
            stored = ""
            try:
                import json as _json

                bj = _json.loads(existing.get("brief_json", "{}"))
                stored = bj.get("response_text", "")
            except Exception:
                logger.debug("Could not parse brief_json for %s", ticker, exc_info=True)
            if len(response_text) > len(stored):
                await tm.update_response_text(existing["id"], response_text)
                logger.info(
                    "Updated brief %s with longer text (%d > %d)",
                    existing["id"],
                    len(response_text),
                    len(stored),
                )
            else:
                logger.debug("Auto-save skipped — brief already exists today for %s", ticker)
            return

    rec, conf = _extract_recommendation_from_text(response_text)

    await tm.store_minimal(
        ticker=ticker,
        user_id=user_id,
        session_id=session.id,
        query=user_query,
        response_text=response_text,
        recommendation=rec,
        confidence=conf,
    )

    pt = PerformanceTracker()
    await pt.record_recommendation(
        ticker=ticker,
        user_id=user_id,
        recommendation=rec,
        confidence=conf,
    )

    logger.info("auto_save_brief: saved %s %s (%.0f%%)", ticker, rec, conf * 100)
    with open(_LOG_FILE, "a") as f:
        f.write(f"  auto_save_brief: {ticker} {rec} ({conf:.0%})\n")


# ADK invokes this after every agent turn. Non-analysis turns are skipped to avoid polluting long-term memory with casual chitchat queries.  # noqa: E501
async def _persist_memory_callback(callback_context) -> None:
    """Persist session to memory after each agent turn.

    This callback is invoked by ADK after every agent invocation.
    It stores the session's events into the memory service so that
    `load_memory` can search across all past sessions.
    Also auto-saves the investment brief when an analysis turn completes.
    """
    session = callback_context.session
    logger.info(
        "_persist_memory_callback: session=%s, events=%s",
        session.id if session else "None",
        len(session.events) if session and session.events else 0,
    )

    with open(_LOG_FILE, "a") as f:
        f.write(
            f"Callback invoked: session={session.id if session else 'None'}, events={len(session.events) if session else 0}\n"  # noqa: E501
        )

    if not session or not session.events:
        logger.warning("No session or no events to persist")
        with open(_LOG_FILE, "a") as f:
            f.write("  No session or no events\n")
        return

    if not _is_analysis_turn(session.events):
        logger.info("Skipping persist + eval — turn did not produce analysis")
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

    # ── Auto-save brief (ADK Web path) ──────────────────────────────────
    user_query, response_text = _extract_query_and_response(session.events)
    if response_text:
        try:
            await _auto_save_brief(session, user_query, response_text)
        except Exception as e:
            logger.error("Auto-save brief failed: %s", e, exc_info=True)
            with open(_LOG_FILE, "a") as f:
                f.write(f"  auto_save_brief failed: {e}\n")

    # ── Orchestrator RAGAS eval (ADK Web path) ──────────────────────────
    # ADK Web bypasses FinSightAgentExecutor, so the eval hook lives here.
    if EVAL_ENABLED and user_query and response_text:
        trace_id = None
        try:
            from shared.observability import get_langfuse_client

            trace_id = get_langfuse_client().get_current_trace_id()
        except Exception:
            logger.debug("Could not get Langfuse trace_id", exc_info=True)
        asyncio.create_task(_eval_score_response(user_query, response_text, trace_id))
        asyncio.create_task(_release_sub_agent_evals())
        logger.info("Orchestrator eval scheduled (trace=%s)", trace_id)


root_agent.before_agent_callback = _memory_cache_callback
root_agent.after_agent_callback = _persist_memory_callback
