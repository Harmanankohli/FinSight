import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import (
    compute_metrics_node,
    correlation_node,
    dcf_valuation_node,
    fetch_price_data_node,
    format_output_node,
    fundamental_analysis_node,
    llm_summary_node,
    stress_test_node,
    technical_analysis_node,
)
from .state import QuantAnalysisState

logger = logging.getLogger(__name__)


def _route_on_volatility(state: QuantAnalysisState) -> str:
    # Conditional edge: high volatility (>35%) → stress test (tail-risk focused), low → DCF (fundamental value)
    if state.get("is_high_volatility", False):
        logger.info("Routing %s to stress_test (volatility=%.4f high)", state.get("ticker"), state.get("volatility", 0))
        return "stress_test"
    logger.info("Routing %s to dcf (volatility=%.4f low)", state.get("ticker"), state.get("volatility", 0))
    return "dcf"


class QuantAnalysisGraph:
    def __init__(self):
        self._graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """Fan-out from START: prices + fundamentals in parallel; technicals runs after prices."""
        builder = StateGraph(QuantAnalysisState)

        builder.add_node("fetch_prices", fetch_price_data_node)
        builder.add_node("fetch_fundamentals", fundamental_analysis_node)
        builder.add_node("compute_base_metrics", compute_metrics_node)
        builder.add_node("technical_analysis", technical_analysis_node)
        builder.add_node("run_stress_test", stress_test_node)
        builder.add_node("run_dcf", dcf_valuation_node)
        builder.add_node("portfolio_correlation", correlation_node)
        builder.add_node("format_output", format_output_node)
        builder.add_node("llm_summary", llm_summary_node)

        # Parallel fan-out from START: price pipeline and fundamentals pipeline
        builder.add_edge(START, "fetch_prices")
        builder.add_edge(START, "fetch_fundamentals")

        # Price path: fetch_prices → compute + technicals in parallel
        builder.add_edge("fetch_prices", "compute_base_metrics")
        builder.add_edge("fetch_prices", "technical_analysis")

        # Volatility-gated branch: high vol → stress test, low → DCF
        builder.add_conditional_edges(
            "compute_base_metrics",
            _route_on_volatility,
            {"stress_test": "run_stress_test", "dcf": "run_dcf"},
        )

        # All three branches fan-in at portfolio_correlation
        builder.add_edge("run_stress_test", "portfolio_correlation")
        builder.add_edge("run_dcf", "portfolio_correlation")
        builder.add_edge("technical_analysis", "portfolio_correlation")

        # Fan-in at format_output: waits for correlation + fundamentals
        builder.add_edge("portfolio_correlation", "format_output")
        builder.add_edge("fetch_fundamentals", "format_output")

        builder.add_edge("format_output", "llm_summary")
        builder.add_edge("llm_summary", END)

        return builder.compile()

    async def run(
        self, ticker: str, period: str = "5y", portfolio_holdings: list[str] | None = None,
        mcp_client: Any | None = None, langfuse_handler: Any | None = None,
    ) -> dict:
        initial: QuantAnalysisState = {
            "ticker": ticker.upper(),
            "period": period,
            "portfolio_holdings": portfolio_holdings or [],
            "price_data": {},
            "messages": [],
            "volatility": 0.0,
            "is_high_volatility": False,
            "metrics": {},
            "stress_test_result": None,
            "dcf_valuation": None,
            "dcf_error": None,
            "correlation_matrix": {},
            "fundamentals": None,
            "technicals": None,
            "_financials_raw": {},
            "recommendation": "",
            "reasoning": "",
            "mcp_client": mcp_client,
        }

        logger.info("Starting graph execution for %s (period=%s, holdings=%d)", ticker, period, len(portfolio_holdings or []))
        config = {}
        if langfuse_handler:
            config["callbacks"] = [langfuse_handler]
        result = await self._graph.ainvoke(initial, config=config)

        dcf_error = result.get("dcf_error")
        if dcf_error:
            logger.warning("DCF failed for %s: %s", ticker.upper(), dcf_error)
        logger.info("Graph execution complete for %s: rec=%s, dcf=%s, stress=%s",
                     ticker.upper(), result.get("recommendation"),
                     "ok" if result.get("dcf_valuation") else "null",
                     "ok" if result.get("stress_test_result") else "null")

        return {
            "ticker": ticker.upper(),
            "recommendation": result.get("recommendation", "HOLD"),
            "reasoning": result.get("reasoning", ""),
            "metrics": result.get("metrics", {}),
            "dcf_valuation": result.get("dcf_valuation"),
            "dcf_error": dcf_error,
            "stress_test": result.get("stress_test_result"),
            "correlation_matrix": result.get("correlation_matrix", {}),
            "fundamentals": result.get("fundamentals"),
            "technicals": result.get("technicals"),
        }
