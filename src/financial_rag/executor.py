import asyncio
import json
import logging
from collections.abc import AsyncIterable
from datetime import UTC, date, datetime

from shared.base_agent import BaseAgent
from shared.logging_config import logged, logged_sync
from shared.mcp_client import get_shared_mcp
from shared.memory.store import is_filing_ingested, mark_filing_ingested
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import extract_ticker, resolve_and_validate_ticker
from shared.trace_context import extract_trace_context

from .document_ingestion import DocumentIngestionPipeline
from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


class RAGAgent(BaseAgent):
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Financial RAG Agent",
            description="Retrieves and analyzes financial documents using RAG with ChromaDB and LM Studio",  # noqa: E501
            content_types=["text", "application/json"],
        )
        self.index = FinancialIndexManager()
        self._ingestion: DocumentIngestionPipeline | None = None
        self._last_ingestion: dict[str, date] = {}

    @logged(log_result=False)
    async def _ensure_ingested(self, ticker: str) -> None:
        # Daily dedup: skip if already ingested today (avoids re-fetching filings every call)
        today = datetime.now(UTC).date()
        if self._last_ingestion.get(ticker) == today:
            return
        try:
            mcp = await get_shared_mcp()
        except Exception as e:
            logger.warning("MCP connect failed: %s", e)
            return
        if self._ingestion is None:
            self._ingestion = DocumentIngestionPipeline(self.index)
        try:
            result = await mcp.call_tool_by_name(
                "get_financial_filings",
                {"ticker": ticker, "annual_limit": 3, "quarterly_limit": 4},
            )
            if hasattr(result, "content"):
                for item in result.content:
                    raw = item.text if hasattr(item, "text") else str(item)
                    if not raw or not raw.strip():
                        continue
                    try:
                        data = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        filings = data.get("annual", []) + data.get("quarterly", [])

                        # Filter to un-ingested filings first
                        candidates = []
                        for filing in filings:
                            edgar_url = filing.get("edgar_url")
                            if not edgar_url:
                                continue
                            if await is_filing_ingested(edgar_url):
                                logger.debug("Skipping already-ingested filing: %s", edgar_url[:80])
                                continue
                            candidates.append(filing)

                        # Fetch filing content in parallel
                        async def _fetch_one(f: dict) -> tuple[dict, str]:
                            edgar_url = f.get("edgar_url", "")
                            ix_url = f.get("ix_url")
                            try:
                                cr = await mcp.call_tool_by_name(
                                    "get_filing_content",
                                    {"edgar_url": edgar_url, "ix_url": ix_url},
                                )
                                if hasattr(cr, "content"):
                                    for ci in cr.content:
                                        txt = ci.text if hasattr(ci, "text") else str(ci)
                                        try:
                                            return f, json.loads(txt).get("content", "")
                                        except (json.JSONDecodeError, TypeError):
                                            continue
                            except Exception as ce:
                                logger.warning("Filing fetch failed for %s: %s", edgar_url[:80], ce)
                            return f, ""

                        if candidates:
                            pairs = await asyncio.gather(*[_fetch_one(f) for f in candidates])
                            new_filings = []
                            for f, content in pairs:
                                f["content"] = content
                                new_filings.append(f)
                            if new_filings:
                                self._ingestion.ingest_sec_filings_batch(ticker, new_filings)
                                for filing in new_filings:
                                    if filing.get("edgar_url"):
                                        await mark_filing_ingested(filing["edgar_url"], ticker)
                                logger.info(
                                    "Ingested %d new filing(s) for %s", len(new_filings), ticker
                                )
                        else:
                            logger.info("0 new filings to ingest for %s", ticker)
            self._last_ingestion[ticker] = today
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    @logged(log_result=False)
    async def _ensure_news_ingested(self, ticker: str) -> None:
        """Fetches recent news via MCP and ingests articles into the 'news' ChromaDB collection."""
        today = datetime.now(UTC).date()
        news_key = f"news_{ticker}"
        if self._last_ingestion.get(news_key) == today:
            return
        try:
            mcp = await get_shared_mcp()
        except Exception as e:
            logger.warning("MCP connect failed for news ingestion: %s", e)
            return
        if self._ingestion is None:
            self._ingestion = DocumentIngestionPipeline(self.index)
        try:
            result = await mcp.call_tool_by_name(
                "get_news_sentiment", {"ticker": ticker, "limit": 15}
            )
            ingested = 0
            if hasattr(result, "content"):
                for item in result.content:
                    raw = item.text if hasattr(item, "text") else str(item)
                    if not raw or not raw.strip():
                        continue
                    try:
                        data = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        continue
                    articles = []
                    if isinstance(data, dict):
                        articles = data.get("articles", data.get("news", []))
                    elif isinstance(data, list):
                        articles = data
                    for article in articles:
                        if not isinstance(article, dict):
                            continue
                        sentiment = article.get("sentiment", 0)
                        summary = (
                            article.get("summary", "") or f"sentiment={sentiment:+.2f}"
                            if sentiment
                            else ""
                        )
                        self._ingestion.ingest_news_article(
                            ticker,
                            {
                                "title": article.get("title", ""),
                                "summary": summary,
                                "url": article.get("link", article.get("url", "")),
                                "published_at": article.get(
                                    "published", article.get("published_at", "")
                                ),
                            },
                        )
                        ingested += 1
            if ingested > 0:
                logger.info("Ingested %d news articles for %s", ingested, ticker)
            self._last_ingestion[news_key] = today
        except Exception as e:
            logger.warning("News ingestion failed for %s: %s", ticker, e)

    @logged()
    async def query(self, ticker: str, query_text: str) -> dict:
        """Await ingestion (if needed) then query. Used by direct callers."""
        await self._ensure_ingested(ticker)
        await self._ensure_news_ingested(ticker)
        return await self.index.query(ticker, query_text)

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        logger.info("stream() called for query=%s...", query[:100])
        ticker = extract_ticker(query)
        if ticker:
            await self._ensure_ingested(ticker)
            await self._ensure_news_ingested(ticker)

        logger.info("stream() complete for query=%s...", query[:100])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        async with self._telemetry_span("rag-agent-stream", query) as (trace_ctx, span, trace_id):
            ticker, company = await resolve_and_validate_ticker(query)
            if not ticker:
                span.update(output={"error": company or "No ticker found"})
                return self._error_response(company or "Could not identify a stock ticker.")

            try:
                result = await self.query(ticker, query)

                # Augment context_texts with web search results
                try:
                    mcp = await get_shared_mcp()
                    web_res = await mcp.call_tool_by_name(
                        "web_search",
                        {
                            "query": f"{ticker} recent news analysis",
                            "max_results": 5,
                            "time_filter": "w",
                        },
                    )
                    if hasattr(web_res, "content") and web_res.content:
                        raw = (
                            web_res.content[0].text
                            if hasattr(web_res.content[0], "text")
                            else str(web_res.content[0])
                        )  # noqa: E501
                        web_data = json.loads(raw)
                        for r in web_data.get("results") or []:
                            snippet = r.get("snippet", "")
                            if snippet:
                                result.setdefault("context_texts", []).append(
                                    f"[Web] {r.get('title', '')}: {snippet}"
                                )  # noqa: E501
                except Exception as we:
                    logger.debug("Web search augmentation failed: %s", we)

                span.update(
                    output={
                        "ticker": ticker,
                        "result_keys": list(result.keys())
                        if isinstance(result, dict)
                        else "unknown",
                    }
                )
                if EVAL_ENABLED:
                    from shared.eval_gate import defer_eval
                    from shared.runtime_eval import score_rag_response as _eval_rag_response

                    defer_eval(
                        _eval_rag_response,
                        query,
                        result.get("summary", ""),
                        result.get("context_texts", []),
                        trace_id,
                    )
                return self._data_response(result)
            except Exception as e:
                logger.exception("RAG query failed")
                span.update(output={"error": str(e)})
                return self._error_response(f"RAG analysis failed: {e}")
