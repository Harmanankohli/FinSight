from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


def _merge_dict(a: dict, b: dict) -> dict:
    """Merge two dicts — used as a LangGraph reducer for keys written by multiple nodes."""
    if not a:
        return b or {}
    if not b:
        return a
    return {**a, **b}


def _last_nonnull(a: Any, b: Any) -> Any:
    """Return b if non-None, else a — last-writer-wins reducer that preserves prior data."""
    return b if b is not None else a


def _last_str(a: str, b: str) -> str:
    """Return b if non-empty, else a — for string fields written by multiple nodes."""
    return b if b else a


class QuantAnalysisState(TypedDict):
    # Input fields
    ticker: str
    period: str
    portfolio_holdings: list[str]

    # Data pipeline
    price_data: dict
    messages: Annotated[list, add_messages]
    volatility: float
    is_high_volatility: bool

    # Analysis outputs.
    #
    # Several keys below carry Annotated reducers.  The graph has a fan-in
    # at format_output (5 predecessors) where two of them — peer_comparison
    # and analyst_positioning — both stem from fetch_fundamentals and often
    # complete in the same LangGraph checkpoint step.  When that happens
    # LangGraph schedules format_output_node twice in the same step, causing
    # every key it writes to receive two values.  Annotated reducers
    # eliminate the INVALID_CONCURRENT_GRAPH_UPDATE error by declaring how
    # to merge concurrent writes rather than rejecting them.
    #
    # Keys written only once (by a single node in a single step) have no
    # reducer and use the default last-value channel.

    # Written by compute_metrics_node AND format_output_node
    metrics: Annotated[dict, _merge_dict]
    # Written by stress_test_node AND format_output_node
    stress_test_result: Annotated[dict | None, _last_nonnull]
    # Written by compute_metrics_node AND dcf_valuation_node
    dcf_error: Annotated[str | None, _last_nonnull]
    # Written by format_output_node AND llm_summary_node
    reasoning: Annotated[str, _last_str]
    # Written by format_output_node (potentially twice from duplicate fan-in trigger)
    recommendation: Annotated[str, _last_str]

    dcf_valuation: dict | None
    correlation_matrix: dict
    fundamentals: dict | None
    technicals: dict | None
    _financials_raw: dict | None
    monte_carlo: dict | None
    peer_comparison: dict | None
    options_signals: dict | None
    insider_signals: dict | None
    positioning: dict | None
    mcp_client: Any | None
