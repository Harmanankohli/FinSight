import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.mcp_client import MCPClient, MCPServerConfig
from shared.config import MCP_SERVER_URL, MCP_TIMEOUT
from shared.ticker_utils import extract_ticker

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Quant Analysis Agent",
            description="Computes quantitative risk metrics and financial analysis using MCP data and LangGraph",
            content_types=["text", "application/json"],
        )
        self.graph = QuantAnalysisGraph()
        self._mcp: MCPClient | None = None
        self._connected = False

    async def _ensure_connected(self):
        if not self._connected:
            self._mcp = MCPClient(configs=[MCPServerConfig(name="finsight-mcp", url=MCP_SERVER_URL)], timeout=MCP_TIMEOUT)
            await self._mcp.connect_all()
            self._connected = True

    async def analyze(self, ticker: str, period: str = "5y") -> dict:
        await self._ensure_connected()
        return await self.graph.run(ticker, period=period, mcp_client=self._mcp)

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        ticker = extract_ticker(query)

        try:
            result = await self.analyze(ticker)
            yield {
                "response_type": "data",
                "is_task_complete": True,
                "require_user_input": False,
                "content": result,
            }
        except Exception as e:
            logger.exception("Quant analysis failed")
            yield {
                "response_type": "text",
                "is_task_complete": True,
                "is_error": True,
                "require_user_input": False,
                "content": f"Quant analysis failed: {e}",
            }
