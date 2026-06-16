"""Pydantic output models for each sub-agent in the multi-agent investment pipeline.

Each agent validates its output through its model before returning via A2A.
The orchestrator validates combined agent outputs through ValidatedAgentOutputs
before passing to report generation, replacing ~220 lines of manual dict extraction.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Quant Agent nested models ────────────────────────────────────────────────


class QuantRiskMetrics(BaseModel):
    """Risk metrics from compute_metrics_node + signal scores from format_output_node."""

    sharpe_ratio: float = 0.0
    annual_volatility: float = 0.0
    beta: float = 0.0
    var_95_daily: float = 0.0
    max_drawdown: float = 0.0
    quant_confidence: Optional[float] = None
    quant_signal: Optional[str] = None
    signals: list[str] = Field(default_factory=list)
    signal_scores: dict[str, float] = Field(default_factory=dict)


class DCFValuation(BaseModel):
    intrinsic_value: float
    current_price: float
    upside_pct: float
    wacc: float
    growth_rate: float
    terminal_growth: float
    enterprise_value: float
    fcf_used: float


class MonteCarloResult(BaseModel):
    p10: float
    p25: Optional[float] = None
    p50: float
    p75: Optional[float] = None
    p90: float
    prob_profit: float
    expected_return_pct: Optional[float] = None
    mc_var_95: Optional[float] = None
    current_price: Optional[float] = None
    n_simulations: Optional[int] = None
    horizon_days: Optional[int] = None


class StressScenario(BaseModel):
    market_decline_pct: Optional[float] = None
    beta_adj_decline_pct: Optional[float] = None
    projected_price: Optional[float] = None
    loss_per_share: Optional[float] = None


class StressTestResult(BaseModel):
    scenarios: dict[str, StressScenario] = Field(default_factory=dict)
    var_95: Optional[float] = None
    cvar_95: Optional[float] = None
    beta_used: Optional[float] = None
    beta_adjusted: bool = True
    # When stress test was skipped:
    note: Optional[str] = None
    volatility: Optional[float] = None
    threshold: Optional[float] = None


class TechnicalIndicators(BaseModel):
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    ema_12: Optional[float] = None
    ema_26: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_bullish: Optional[bool] = None
    rsi: Optional[float] = None
    rsi_14: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_position: Optional[float] = None
    momentum_20d: Optional[float] = None
    momentum_60d: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    trend: Optional[str] = None
    golden_cross: Optional[bool] = None
    above_50d_ma: Optional[bool] = None
    above_200d_ma: Optional[bool] = None

    @property
    def rsi_value(self) -> Optional[float]:
        """Return rsi_14 if set, fall back to rsi."""
        return self.rsi_14 if self.rsi_14 is not None else self.rsi


class Fundamentals(BaseModel):
    model_config = {"populate_by_name": True}

    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    price_to_sales: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    ev_to_revenue: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    profit_margin: Optional[float] = None
    roe: Optional[float] = None
    roa: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    earnings_quarterly_growth: Optional[float] = None
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    quick_ratio: Optional[float] = None
    total_debt: Optional[float] = None
    total_cash: Optional[float] = None
    market_cap: Optional[float] = None
    dividend_yield: Optional[float] = None
    payout_ratio: Optional[float] = None
    # Digit-prefixed keys — alias matches the actual dict key from quant/nodes/data_fetch.py
    high_52w: Optional[float] = Field(None, alias="52w_high")
    low_52w: Optional[float] = Field(None, alias="52w_low")
    avg_50d: Optional[float] = Field(None, alias="50d_avg")
    avg_200d: Optional[float] = Field(None, alias="200d_avg")
    current_price: Optional[float] = None
    pct_from_52w_high: Optional[float] = None
    pct_from_52w_low: Optional[float] = None
    golden_cross: Optional[bool] = None
    net_debt: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None


class PeerComparison(BaseModel):
    industry: Optional[str] = None
    sector: Optional[str] = None
    peers: list[str] = Field(default_factory=list)
    comparison: dict[str, dict] = Field(default_factory=dict)
    rankings: dict[str, int] = Field(default_factory=dict)
    n_peers: int = 0
    medians: dict[str, float] = Field(default_factory=dict)
    note: Optional[str] = None


class OptionsSignals(BaseModel):
    put_call_volume_ratio: Optional[float] = None
    put_call_oi_ratio: Optional[float] = None
    call_volume: int = 0
    put_volume: int = 0
    total_volume: int = 0
    flow_signal: str = "no_data"
    note: Optional[str] = None


class InsiderSignals(BaseModel):
    recent_transaction_count: int = 0
    buy_signals: int = 0
    sell_signals: int = 0
    direction: str = "neutral"
    net_shares: Optional[float] = None
    net_value: Optional[float] = None
    insider_pct_held: Optional[float] = None
    activity_level: str = "low"


class AnalystPositioning(BaseModel):
    recommendation_key: Optional[str] = None
    consensus_score: int = 0
    n_analysts: Optional[int] = None
    analyst_target_price: Optional[float] = None
    analyst_upside_pct: Optional[float] = None
    short_ratio: Optional[float] = None
    short_pct_float: Optional[float] = None
    earnings_surprise_est: Optional[float] = None
    short_squeeze_risk: bool = False


class QuantAgentOutput(BaseModel):
    """Complete validated output of the Quant Analysis Agent (LangGraph)."""

    ticker: str
    recommendation: str = "HOLD"
    reasoning: str = ""
    metrics: QuantRiskMetrics = Field(default_factory=QuantRiskMetrics)
    dcf_valuation: Optional[DCFValuation] = None
    dcf_error: Optional[str] = None
    stress_test: Optional[StressTestResult] = None
    monte_carlo: Optional[MonteCarloResult] = None
    correlation_matrix: dict = Field(default_factory=dict)
    fundamentals: Optional[Fundamentals] = None
    technicals: Optional[TechnicalIndicators] = None
    peer_comparison: Optional[PeerComparison] = None
    options_signals: Optional[OptionsSignals] = None
    insider_signals: Optional[InsiderSignals] = None
    positioning: Optional[AnalystPositioning] = None
    schema_validation: Optional[dict] = None


# ── RAG Agent model ──────────────────────────────────────────────────────────


class RAGAgentOutput(BaseModel):
    """Validated output of the Financial RAG Agent (LlamaIndex)."""

    ticker: str
    summary: str = ""
    sources: list = Field(default_factory=list)
    relevance_scores: list[float] = Field(default_factory=list)
    confidence_score: float = 0.0
    context_texts: list[str] = Field(default_factory=list)


# ── Market Context Agent models ──────────────────────────────────────────────


class MarketContextPeer(BaseModel):
    ticker: str
    metrics: dict[str, str] = Field(default_factory=dict)


class MarketContextOutput(BaseModel):
    """Validated output of the Market Context Agent (CrewAI)."""

    narrative: str = ""
    overall_signal: str = "neutral"
    confidence_score: float = 0.0
    key_tailwinds: list[str] = Field(default_factory=list)
    key_headwinds: list[str] = Field(default_factory=list)
    macro_regime: Optional[str] = None
    relative_peer_positioning: Optional[str] = None
    peer_comparison: list[MarketContextPeer] = Field(default_factory=list)


# ── Analytics Agent models ──────────────────────────────────────────────────


class TrendAnalysis(BaseModel):
    trend_direction: str = "neutral"
    ma_crossover_signal: Optional[str] = None
    momentum_shift: Optional[str] = None
    trend_strength: float = 0.0
    supporting_indicators: list[str] = Field(default_factory=list)


class ForecastResult(BaseModel):
    method: str = "exponential_smoothing"
    horizon_days: int = 30
    forecast_prices: list[float] = Field(default_factory=list)
    forecast_dates: list[str] = Field(default_factory=list)
    confidence_lower: list[float] = Field(default_factory=list)
    confidence_upper: list[float] = Field(default_factory=list)
    mape: Optional[float] = None


class ChartPayload(BaseModel):
    chart_type: str = "candlestick"
    labels: list[str] = Field(default_factory=list)
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class StatisticalSummary(BaseModel):
    return_distribution: Optional[str] = None
    skewness: Optional[float] = None
    kurtosis: Optional[float] = None
    jarque_bera_pvalue: Optional[float] = None
    correlations: dict[str, float] = Field(default_factory=dict)
    regression_beta: Optional[float] = None
    regression_r_squared: Optional[float] = None


class AnomalyReport(BaseModel):
    price_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    volume_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    fundamental_anomalies: list[str] = Field(default_factory=list)
    anomaly_count: int = 0
    severity: str = "none"


class AnalyticsAgentOutput(BaseModel):
    ticker: str
    trend_analysis: Optional[TrendAnalysis] = None
    forecast: Optional[ForecastResult] = None
    charts: list[ChartPayload] = Field(default_factory=list)
    statistical_summary: Optional[StatisticalSummary] = None
    anomalies: Optional[AnomalyReport] = None
    analytics_confidence: float = 0.0
    analytics_signal: str = "neutral"


# ── Reviewer Agent models ───────────────────────────────────────────────────


class ContradictionFlag(BaseModel):
    agents: list[str]
    field: str
    description: str
    severity: str = "low"


class SourceVerification(BaseModel):
    agent_name: str
    claims_checked: int = 0
    claims_verified: int = 0
    verification_rate: float = 0.0
    unverified_claims: list[str] = Field(default_factory=list)


class ConfidenceBreakdown(BaseModel):
    agent_scores: dict[str, float] = Field(default_factory=dict)
    agreement_score: float = 0.0
    data_quality_score: float = 0.0
    meta_confidence: float = 0.0


class RecommendationValidation(BaseModel):
    recommendation: str = "HOLD"
    evidence_supports: bool = True
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    evidence_strength: str = "moderate"


class ReviewerAgentOutput(BaseModel):
    ticker: str
    verdict: str = "HOLD"
    review_summary: str = ""
    contradictions: list[ContradictionFlag] = Field(default_factory=list)
    source_verifications: list[SourceVerification] = Field(default_factory=list)
    confidence_breakdown: Optional[ConfidenceBreakdown] = None
    recommendation_validation: Optional[RecommendationValidation] = None
    flags: list[str] = Field(default_factory=list)
    review_confidence: float = 0.0


# ── Combined report-level model ──────────────────────────────────────────────


class ValidatedAgentOutputs(BaseModel):
    """All agent outputs validated together at the orchestrator level."""

    ticker: str
    quant: Optional[QuantAgentOutput] = None
    rag: Optional[RAGAgentOutput] = None
    market_context: Optional[MarketContextOutput] = None
    analytics: Optional[AnalyticsAgentOutput] = None
    reviewer: Optional[ReviewerAgentOutput] = None

    @property
    def has_all_agents(self) -> bool:
        return all([self.quant, self.rag, self.market_context, self.analytics, self.reviewer])
