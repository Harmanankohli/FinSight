import asyncio
import json
import logging
from collections.abc import AsyncIterable
from datetime import date, datetime, timezone

from shared.base_agent import BaseAgent
from shared.mcp_client import get_shared_mcp
from shared.config import EVAL_ENABLED
from shared.memory.store import is_filing_ingested, mark_filing_ingested
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_rag_response as _eval_rag_response
from shared.ticker_utils import extract_ticker, validate_ticker_via_mcp, resolve_ticker_via_mcp
from shared.trace_context import extract_trace_ids

from .document_ingestion import DocumentIngestionPipeline
from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


class RAGAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Financial RAG Agent",
            description="Retrieves and analyzes financial documents using RAG with ChromaDB and LM Studio",
            content_types=["text", "application/json"],
        )
        self.index = FinancialIndexManager()
        self._ingestion: DocumentIngestionPipeline | None = None
        self._last_ingestion: dict[str, date] = {}

    async def _ensure_ingested(self, ticker: str) -> None:
        # Daily dedup: skip if already ingested today (avoids re-fetching filings every call)
        today = datetime.now(timezone.utc).date()
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
                "get_company_filings",
                {"ticker": ticker, "form_types": "10-K,10-Q,8-K", "limit": 5},
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
                        filings = data.get("filings", [])
                        new_filings = []
                        for filing in filings:
                            edgar_url = filing.get("edgar_url")
                            ix_url = filing.get("ix_url")
                            # Skip filings already ingested (persistent dedup across restarts)
                            if edgar_url and await is_filing_ingested(edgar_url):
                                logger.debug("Skipping already-ingested filing: %s", edgar_url[:80])
                                continue
                            if edgar_url:
                                try:
                                    content_result = await mcp.call_tool_by_name(
                                        "get_filing_content",
                                        {"edgar_url": edgar_url, "ix_url": ix_url},
                                    )
                                    if hasattr(content_result, "content"):
                                        content_text = ""
                                        for citem in content_result.content:
                                            cres = citem.text if hasattr(citem, "text") else str(citem)
                                            try:
                                                cdata = json.loads(cres)
                                                content_text = cdata.get("content", "")
                                                break
                                            except (json.JSONDecodeError, TypeError):
                                                continue
                                    else:
                                        content_text = ""
                                except Exception as ce:
                                    logger.warning("Failed to fetch filing content for %s: %s", edgar_url, ce)
                                    content_text = ""
                            else:
                                content_text = ""
                            filing["content"] = content_text
                            new_filings.append(filing)
                        if new_filings:
                            self._ingestion.ingest_sec_filings_batch(ticker, new_filings)
                            for filing in new_filings:
                                if filing.get("edgar_url"):
                                    await mark_filing_ingested(filing["edgar_url"], ticker)
                            logger.info("Ingested %d new filing(s) for %s", len(new_filings), ticker)
                        else:
                            logger.info("0 new filings to ingest for %s", ticker)
            self._last_ingestion[ticker] = today
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    async def query(self, ticker: str, query_text: str) -> dict:
        # Main entry point: auto-ingest today's filings then query ChromaDB via LlamaIndex
        await self._ensure_ingested(ticker)
        return await self.index.query(ticker, query_text)

    async def _validate_ticker(self, ticker: str) -> tuple[bool, str, str]:
        if not ticker:
            return False, "", "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."
        try:
            mcp = await get_shared_mcp()
            return await validate_ticker_via_mcp(mcp, ticker)
        except Exception as e:
            logger.warning("Ticker validation via MCP failed, proceeding with regex guess: %s", e)
            return True, ticker, ""

    async def _resolve_ticker(self, query: str, exclude_ticker: str = "") -> tuple[str, str]:
        try:
            mcp = await get_shared_mcp()
            return await resolve_ticker_via_mcp(mcp, query, exclude_ticker)
        except Exception as e:
            logger.warning("MCP ticker resolution failed: %s", e)
            return "", ""

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        yield await self._build_response(query)

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
            name="rag-agent-stream",
            input=query,
            trace_context=trace_ctx,
        ) as span:
            # Fallback chain: regex extract → MCP resolve → MCP validate
            ticker = extract_ticker(query)
            resolved = False

            if not ticker:
                ticker, _ = await self._resolve_ticker(query)
                resolved = True

            if not ticker:
                span.update(output={"error": "No ticker found"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",
                }

            valid, validated_ticker, company = await self._validate_ticker(ticker)
            if not valid and not resolved:
                ticker, _ = await self._resolve_ticker(query, exclude_ticker=ticker)
                if ticker:
                    valid, validated_ticker, company = await self._validate_ticker(ticker)

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
                result = await self.query(ticker, query)
                span.update(output={"ticker": ticker, "result_keys": list(result.keys()) if isinstance(result, dict) else "unknown"})
                if EVAL_ENABLED:
                    asyncio.create_task(
                        _eval_rag_response(
                            query,
                            result.get("summary", ""),
                            result.get("context_texts", []),
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
                logger.exception("RAG query failed")
                span.update(output={"error": str(e)})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"RAG analysis failed: {e}",
                }
