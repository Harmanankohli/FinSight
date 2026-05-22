import json
import logging
from collections.abc import AsyncIterable
from datetime import date, datetime, timezone

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL
from shared.observability import get_langfuse_client
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
        self._mcp: MCPClient | None = None
        self._ingestion: DocumentIngestionPipeline | None = None
        self._last_ingestion: dict[str, date] = {}

    async def _ensure_ingested(self, ticker: str) -> None:
        today = datetime.now(timezone.utc).date()
        if self._last_ingestion.get(ticker) == today:
            return
        if not await self._ensure_mcp_connected():
            return
        if self._ingestion is None:
            self._ingestion = DocumentIngestionPipeline(self.index)
        try:
            result = await self._mcp.call_tool_by_name(
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
                        for filing in filings:
                            edgar_url = filing.get("edgar_url")
                            ix_url = filing.get("ix_url")
                            if edgar_url:
                                try:
                                    content_result = await self._mcp.call_tool_by_name(
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
                        self._ingestion.ingest_sec_filings_batch(ticker, filings)
            self._last_ingestion[ticker] = today
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    async def query(self, ticker: str, query_text: str) -> dict:
        await self._ensure_ingested(ticker)
        return await self.index.query(ticker, query_text)

    async def _ensure_mcp_connected(self) -> bool:
        if self._mcp is None:
            self._mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)])
            try:
                await self._mcp.connect_all()
            except Exception as e:
                logger.warning("MCP connect failed: %s", e)
                self._mcp = None
                return False
        return True

    async def _validate_ticker(self, ticker: str) -> tuple[bool, str, str]:
        if not ticker:
            return False, "", "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V)."
        if not await self._ensure_mcp_connected():
            return True, ticker, ""
        try:
            return await validate_ticker_via_mcp(self._mcp, ticker)
        except Exception as e:
            logger.warning("Ticker validation via MCP failed, proceeding with regex guess: %s", e)
            return True, ticker, ""

    async def _resolve_ticker(self, query: str, exclude_ticker: str = "") -> tuple[str, str]:
        if not await self._ensure_mcp_connected():
            return "", ""
        try:
            return await resolve_ticker_via_mcp(self._mcp, query, exclude_ticker)
        except Exception as e:
            logger.warning("MCP ticker resolution failed: %s", e)
            return "", ""

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        span = langfuse.start_observation(
            as_type="span",
            name="rag-agent-stream",
            input=query,
            trace_context=trace_ctx,
        )
        try:
            ticker = extract_ticker(query)
            resolved = False

            if not ticker:
                ticker, _ = await self._resolve_ticker(query)
                resolved = True

            if not ticker:
                span.update(output={"error": "No ticker found"})
                yield {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": "Could not identify a stock ticker from the query. Try using parentheses (AAPL) or $ prefix ($V).",
                }
                return

            valid, validated_ticker, company = await self._validate_ticker(ticker)
            if not valid and not resolved:
                ticker, _ = await self._resolve_ticker(query, exclude_ticker=ticker)
                if ticker:
                    valid, validated_ticker, company = await self._validate_ticker(ticker)

            if not valid:
                span.update(output={"error": f"Invalid ticker: {ticker}"})
                yield {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Ticker '{ticker}' is not valid. Error: {company}",
                }
                return

            ticker = validated_ticker

            try:
                result = await self.query(ticker, query)
                span.update(output={"ticker": ticker, "result_keys": list(result.keys()) if isinstance(result, dict) else "unknown"})
                yield {
                    "response_type": "data",
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": result,
                }
            except Exception as e:
                logger.exception("RAG query failed")
                span.update(output={"error": str(e)})
                yield {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"RAG analysis failed: {e}",
                }
        finally:
            span.end()
