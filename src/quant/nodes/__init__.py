"""Public API for quant graph nodes — re-exports all node functions.

Each node is a callable that takes state + MCP client and returns
an updated state dict.  Used by QuantAnalysisGraph._build_graph()
to register nodes in the LangGraph state machine.
"""

from .data_fetch import (
    analyst_positioning_node,
    fetch_price_data_node,
    fetch_web_context_node,
    fundamental_analysis_node,
    insider_signals_node,
    options_flow_node,
)
from .dcf import dcf_valuation_node
from .monte_carlo import stress_test_node
from .portfolio import correlation_node
from .summary import format_output_node, llm_summary_node, peer_comparison_node
from .technical import compute_metrics_node, technical_analysis_node

__all__ = [
    "analyst_positioning_node",
    "compute_metrics_node",
    "correlation_node",
    "dcf_valuation_node",
    "fetch_price_data_node",
    "fetch_web_context_node",
    "format_output_node",
    "fundamental_analysis_node",
    "insider_signals_node",
    "llm_summary_node",
    "options_flow_node",
    "peer_comparison_node",
    "stress_test_node",
    "technical_analysis_node",
]
