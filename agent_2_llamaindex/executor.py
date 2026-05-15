import json
import logging
import re
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL

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

    async def _ensure_ingested(self, ticker: str) -> None:
        if self._mcp is None:
            self._mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)])
            try:
                await self._mcp.connect_all()
            except Exception as e:
                logger.warning("MCP connect failed (non-fatal): %s", e)
                self._mcp = None
                return
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
                        self._ingestion.ingest_sec_filings_batch(
                            ticker, data.get("filings", [])
                        )
        except Exception as e:
            logger.warning("Auto-ingest failed for %s: %s", ticker, e)

    async def query(self, ticker: str, query_text: str) -> dict:
        await self._ensure_ingested(ticker)
        return await self.index.query(ticker, query_text)

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        ticker_match = re.search(r"\b[A-Z]{2,5}\b", query)
        ticker = ticker_match.group(0) if ticker_match else ""

        try:
            result = await self.query(ticker, query)
            yield {
                "response_type": "data",
                "is_task_complete": True,
                "require_user_input": False,
                "content": result,
            }
        except Exception as e:
            logger.exception("RAG query failed")
            yield {
                "response_type": "text",
                "is_task_complete": True,
                "require_user_input": False,
                "content": f"RAG analysis failed: {e}",
            }
