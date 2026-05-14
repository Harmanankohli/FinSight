import logging
import re
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent

from .graph import QuantAnalysisGraph

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Quant Analysis Agent",
            description="Computes quantitative risk metrics and financial analysis using yfinance and LangGraph",
            content_types=["text", "application/json"],
        )
        self.graph = QuantAnalysisGraph()

    async def analyze(self, ticker: str, period: str = "5y") -> dict:
        return await self.graph.run(ticker, period=period)

    async def stream(
        self, query: str, context_id: str, task_id: str
    ) -> AsyncIterable[dict]:
        ticker_match = re.search(r"\b[A-Z]{2,5}\b", query)
        ticker = ticker_match.group(0) if ticker_match else ""

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
                "require_user_input": False,
                "content": f"Quant analysis failed: {e}",
            }
