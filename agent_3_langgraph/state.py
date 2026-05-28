from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class QuantAnalysisState(TypedDict):
    # Input fields
    ticker: str  # Target equity symbol for analysis
    period: str  # Lookback window (e.g. "5y", "1y") for price data fetching
    portfolio_holdings: list[str]  # Peer tickers for pairwise correlation analysis

    # Data pipeline
    price_data: dict  # Raw price series from MCP → parsed {date: close} dict
    messages: Annotated[list, add_messages]  # LangGraph message accumulator (for LLM integration)
    volatility: float  # Annualized volatility computed from price returns
    is_high_volatility: bool  # Threshold gate (>35%) → routes to stress test vs DCF

    # Analysis outputs
    metrics: dict  # Sharpe, Vol, Beta, VaR, Max Drawdown
    stress_test_result: dict | None  # Scenario projections (2008/2020/dot-com/recession) + CVaR
    dcf_valuation: dict | None  # 5Y DCF intrinsic value, upside %, terminal value assumptions
    dcf_error: str | None  # Human-readable reason when DCF is skipped (e.g. no FCF, high vol)
    correlation_matrix: dict  # Pairwise Pearson correlations across portfolio holdings
    fundamentals: dict | None  # PE, ROE, margins, growth, D/E — from get_financials info
    technicals: dict | None  # RSI, MACD, trend, support/resistance — computed from price_data
    _financials_raw: dict | None  # Raw get_financials response, reused by DCF to avoid double call
    monte_carlo: dict | None  # GBM MC sim: p10/p25/p50/p75/p90, prob_profit, mc_var_95
    peer_comparison: dict | None  # Sector peer ranks on PE, EV/EBITDA, RevGrowth, OpMargin, ROE
    options_signals: dict | None  # Put/call volume ratio, OI ratio, flow signal
    insider_signals: dict | None  # Form 4 net buy/sell count, activity level, insider % held
    positioning: dict | None  # Analyst consensus score, price target upside, short interest
    recommendation: str  # Signal vote: BUY / HOLD / SELL
    reasoning: str  # Compact string of all signals for downstream use (or LLM summary)
    mcp_client: Any | None  # Shared MCP connection, injected at graph entry, closed by caller
