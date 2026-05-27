import asyncio
import json
import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.logging_config import logged
from shared.config import EVAL_ENABLED
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_sentiment_response as _eval_sentiment_response
from shared.mcp_client import get_shared_mcp
from shared.ticker_utils import extract_ticker, validate_ticker, resolve_ticker
from shared.trace_context import extract_trace_ids

from .crew import SentimentIntelligenceCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


class SentimentAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Sentiment Intelligence Agent",
            description="Synthesizes financial news sentiment, SEC insider trading signals, and investment narratives using CrewAI",
            content_types=["text", "application/json"],
        )

    async def _collect_data_parallel(self, ticker: str, mcp) -> dict:
        # Concurrent fetch: fires news sentiment and SEC filings requests in parallel via asyncio.gather
        results = {}

        async def call(tool, args):
            try:
                r = await mcp.call_tool_by_name(tool, args)
                if hasattr(r, "content"):
                    for item in r.content:
                        txt = item.text if hasattr(item, "text") else str(item)
                        try:
                            return json.loads(txt)
                        except (json.JSONDecodeError, TypeError):
                            return {"raw": txt[:500]}
                return {"raw": str(r)[:500]}
            except Exception as e:
                return {"error": str(e)}

        tasks = {
            "news": call("get_news_sentiment", {"ticker": ticker, "limit": 5}),
            "filings": call("get_company_filings", {"ticker": ticker, "form_types": "", "limit": 3}),
        }
        done = await asyncio.gather(*[asyncio.create_task(t) for t in tasks.values()], return_exceptions=True)
        for name, result in zip(tasks.keys(), done):
            if isinstance(result, Exception):
                results[name] = {"error": str(result)}
            else:
                results[name] = result
        return results

    async def analyze(self, ticker: str, query_text: str) -> dict:
        # Orchestration: parallel data collection → CrewAI analysis → extract contexts for RAGAS eval
        mcp = await get_shared_mcp()
        data = await self._collect_data_parallel(ticker, mcp)
        logger.info("Collected data for %s: %s", ticker, list(data.keys()))
        wrapper = MCPClientWrapper(mcp)
        crew_builder = SentimentIntelligenceCrew(wrapper)
        result = await crew_builder.analyze(ticker, precollected_data=data)
        result["_retrieved_contexts"] = _extract_sentiment_contexts(data)
        return result

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        with langfuse.start_as_current_observation(
            as_type="span",
            name="sentiment-agent-stream",
            input=query,
            trace_context=trace_ctx,
        ) as span:
            ticker = extract_ticker(query)
            resolved = False

            if not ticker:
                span.update(output={"error": "No ticker found"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",
                }

            valid, validated_ticker, company = await validate_ticker(ticker)
            if not valid and not resolved:
                ticker, _ = await resolve_ticker(query, ticker)
                if ticker:
                    valid, validated_ticker, company = await validate_ticker(ticker)

            if not valid:
                span.update(output={"error": f"Invalid ticker: {ticker}"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Ticker '{ticker}' is not valid. Error: {company}",
                }

            ticker = validated_ticker

            try:
                result = await self.analyze(ticker, query)
                contexts = result.pop("_retrieved_contexts", [])
                span.update(output={
                    "ticker": ticker,
                    "signal": result.get("overall_signal"),
                    "confidence": result.get("confidence_score"),
                })
                narrative = (
                    result.get("narrative")
                    or result.get("investment_narrative")
                    or result.get("analysis")
                    or json.dumps(result, indent=2)
                )
                if EVAL_ENABLED:
                    asyncio.create_task(
                        _eval_sentiment_response(
                            query,
                            narrative,
                            contexts,
                            trace_id,
                        )
                    )
                return {
                    "response_type": "data",
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": result,
                }
            except Exception as e:
                logger.exception("Sentiment analysis failed")
                span.update(output={"error": str(e)})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Sentiment analysis failed: {e}",
                }


def _extract_sentiment_contexts(data: dict) -> list[str]:
    # Builds RAGAS faithfulness inputs: flat text strings from news articles and filing descriptions
    contexts: list[str] = []

    news = data.get("news", {})
    articles = news.get("articles", []) if isinstance(news, dict) else []
    for a in articles[:5]:
        title = a.get("title", "")
        summary = a.get("summary", "") or a.get("content", "")
        sentiment = a.get("compound", a.get("sentiment_score", ""))
        if title:
            contexts.append(
                f"{title}: {summary[:200]} (sentiment score: {sentiment})"
                if summary else f"{title} (sentiment score: {sentiment})"
            )

    filings = data.get("filings", {})
    filing_list = filings.get("filings", []) if isinstance(filings, dict) else []
    for f in filing_list[:3]:
        form = f.get("form_type", f.get("form", ""))
        date_ = f.get("filing_date", f.get("date", ""))
        desc = f.get("description", "")
        if form:
            contexts.append(f"{form} filing ({date_}): {desc}" if desc else f"{form} filing ({date_})")

    return contexts
