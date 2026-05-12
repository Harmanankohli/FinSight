from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class QuantAnalysisState(TypedDict):
    ticker: str
    period: str
    portfolio_holdings: list[str]

    price_data: dict
    messages: Annotated[list, add_messages]
    volatility: float
    is_high_volatility: bool

    metrics: dict
    stress_test_result: dict | None
    dcf_valuation: dict | None
    correlation_matrix: dict
    recommendation: str
    reasoning: str
