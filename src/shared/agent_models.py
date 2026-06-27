"""Pydantic output models for each sub-agent in the multi-agent investment pipeline.

Each agent validates its output through its model before returning via A2A.
The orchestrator validates combined agent outputs through ValidatedAgentOutputs
before passing to report generation, replacing ~220 lines of manual dict extraction.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# ── Quant Agent nested models ────────────────────────────────────────────────


class QuantRiskMetrics(BaseModel):
    """Risk metrics from compute_metrics_node + signal scores from format_output_node."""

    sharpe_ratio: float = Field(0.0, ge=-5, le=5)
    annual_volatility: float = Field(0.0, ge=0)
    beta: float = Field(0.0, ge=-3, le=6)
    var_95_daily: float = Field(0.0, ge=-1, le=0)
    max_drawdown: float = Field(0.0, ge=-1, le=0)
    sortino_ratio: float = Field(0.0, ge=-5, le=5)
    calmar_ratio: float = Field(0.0, ge=-5, le=5)
    alpha: float = Field(0.0, ge=-5, le=5)
    information_ratio: float = Field(0.0, ge=-5, le=5)
    quant_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    quant_signal: Optional[str] = None
    signals: list[str] = Field(default_factory=list)
    signal_scores: dict[str, float] = Field(default_factory=dict)


class DCFValuation(BaseModel):
    intrinsic_value: float = Field(gt=0)
    current_price: float = Field(gt=0)
    upside_pct: float
    method: str = "dcf"
    wacc: Optional[float] = Field(None, ge=0.0, le=1.0)
    growth_rate: Optional[float] = None
    terminal_growth: Optional[float] = Field(None, ge=0.0, le=0.03)
    enterprise_value: Optional[float] = Field(None, gt=0)
    market_enterprise_value: Optional[float] = Field(None, gt=0)
    net_debt: Optional[float] = None
    equity_value: Optional[float] = Field(None, gt=0)
    fcf_used: Optional[float] = Field(None, gt=0)
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    dcf_assumptions: Optional[dict[str, Any]] = None
    fcf_age_days: Optional[float] = Field(None, ge=0)
    freshness: Optional[dict[str, Any]] = None
    valuation_warnings: Optional[list[str]] = None
    scenarios: Optional[dict[str, Any]] = None
    valuation_reliability: Optional[float] = Field(None, ge=0.0, le=1.0)
    pb_ratio_used: Optional[float] = Field(None, gt=0)
    fair_pb_multiple: Optional[float] = Field(None, gt=0)
    sensitivity_matrix: Optional[dict[str, Any]] = None


class MonteCarloResult(BaseModel):
    p10: float = Field(gt=0)
    p25: Optional[float] = Field(None, gt=0)
    p50: float = Field(gt=0)
    p75: Optional[float] = Field(None, gt=0)
    p90: float = Field(gt=0)
    prob_profit: float = Field(ge=0.0, le=1.0)
    expected_return_pct: Optional[float] = None
    mc_var_95: Optional[float] = Field(None, le=0)
    current_price: Optional[float] = Field(None, gt=0)
    n_simulations: Optional[int] = Field(None, gt=0)
    horizon_days: Optional[int] = Field(None, gt=0)


class StressScenario(BaseModel):
    market_decline_pct: Optional[float] = Field(None, le=0)
    beta_adj_decline_pct: Optional[float] = Field(None, le=0)
    projected_price: Optional[float] = Field(None, gt=0)
    loss_per_share: Optional[float] = Field(None, le=0)


class StressTestResult(BaseModel):
    scenarios: dict[str, StressScenario] = Field(default_factory=dict)
    var_95: Optional[float] = Field(None, le=0)
    cvar_95: Optional[float] = Field(None, le=0)
    beta_used: Optional[float] = Field(None, ge=-3, le=6)
    beta_adjusted: bool = True
    # When stress test was skipped:
    note: Optional[str] = None
    volatility: Optional[float] = Field(None, ge=0)
    threshold: Optional[float] = Field(None, ge=0)


class TechnicalIndicators(BaseModel):
    sma_20: Optional[float] = Field(None, gt=0)
    sma_50: Optional[float] = Field(None, gt=0)
    sma_200: Optional[float] = Field(None, gt=0)
    ema_12: Optional[float] = Field(None, gt=0)
    ema_26: Optional[float] = Field(None, gt=0)
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_bullish: Optional[bool] = None
    rsi: Optional[float] = Field(None, ge=0, le=100)
    rsi_14: Optional[float] = Field(None, ge=0, le=100)
    bb_upper: Optional[float] = Field(None, gt=0)
    bb_lower: Optional[float] = Field(None, gt=0)
    bb_position: Optional[float] = Field(None, ge=0.0, le=1.0)
    momentum_20d: Optional[float] = None
    momentum_60d: Optional[float] = None
    support: Optional[float] = Field(None, gt=0)
    resistance: Optional[float] = Field(None, gt=0)
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
    price_to_book: Optional[float] = Field(None, gt=0)
    price_to_sales: Optional[float] = Field(None, gt=0)
    ev_to_ebitda: Optional[float] = None
    ev_to_revenue: Optional[float] = Field(None, gt=0)
    gross_margin: Optional[float] = Field(None, ge=-1.0, le=1.0)
    operating_margin: Optional[float] = Field(None, ge=-1.0, le=1.0)
    profit_margin: Optional[float] = Field(None, ge=-1.0, le=1.0)
    roe: Optional[float] = None
    roa: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    earnings_quarterly_growth: Optional[float] = None
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = Field(None, ge=0)
    quick_ratio: Optional[float] = Field(None, ge=0)
    total_debt: Optional[float] = Field(None, ge=0)
    total_cash: Optional[float] = Field(None, ge=0)
    market_cap: Optional[float] = Field(None, gt=0)
    dividend_yield: Optional[float] = Field(None, ge=0.0, le=1.0)
    payout_ratio: Optional[float] = None
    # Digit-prefixed keys — alias matches the actual dict key from quant/nodes/data_fetch.py
    high_52w: Optional[float] = Field(None, alias="52w_high", gt=0)
    low_52w: Optional[float] = Field(None, alias="52w_low", gt=0)
    avg_50d: Optional[float] = Field(None, alias="50d_avg", gt=0)
    avg_200d: Optional[float] = Field(None, alias="200d_avg", gt=0)
    current_price: Optional[float] = Field(None, gt=0)
    pct_from_52w_high: Optional[float] = None
    pct_from_52w_low: Optional[float] = Field(None, ge=0)
    golden_cross: Optional[bool] = None
    net_debt: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None


class PeerComparison(BaseModel):
    industry: Optional[str] = None
    sector: Optional[str] = None
    peers: list[str] = Field(default_factory=list)
    comparison: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rankings: dict[str, int] = Field(default_factory=dict)
    n_peers: int = Field(0, ge=0)
    medians: dict[str, float] = Field(default_factory=dict)
    note: Optional[str] = None


class OptionsSignals(BaseModel):
    put_call_volume_ratio: Optional[float] = None
    put_call_oi_ratio: Optional[float] = None
    call_volume: int = Field(0, ge=0)
    put_volume: int = Field(0, ge=0)
    total_volume: int = Field(0, ge=0)
    flow_signal: str = "no_data"
    note: Optional[str] = None


class InsiderSignals(BaseModel):
    recent_transaction_count: int = Field(0, ge=0)
    buy_signals: int = Field(0, ge=0)
    sell_signals: int = Field(0, ge=0)
    direction: str = "neutral"
    net_shares: Optional[float] = None
    net_value: Optional[float] = None
    insider_pct_held: Optional[float] = Field(None, ge=0.0, le=1.0)
    activity_level: str = "low"


class AnalystAction(BaseModel):
    """Single analyst upgrade/downgrade/initiation action."""

    date: str = ""
    firm: str = ""
    action: str = ""
    from_grade: str = ""
    to_grade: str = ""
    prior_target: Optional[float] = None
    new_target: Optional[float] = None
    target_action: str = ""


class AnalystActivityResponse(BaseModel):
    """Response from get_analyst_activity MCP tool."""

    ticker: str
    activities: list[AnalystAction] = Field(default_factory=list)
    total: int = 0
    upgrades: int = 0
    downgrades: int = 0
    initiations: int = 0
    error: Optional[str] = None


class ForwardEstimate(BaseModel):
    """Single forward EPS/revenue estimate period."""

    period: Optional[str] = None
    end_date: Optional[str] = None
    growth: Optional[float] = None
    eps_avg: Optional[float] = None
    eps_low: Optional[float] = None
    eps_high: Optional[float] = None
    eps_year_ago: Optional[float] = None
    n_analysts: Optional[int] = None
    revenue_avg: Optional[float] = None
    revenue_growth: Optional[float] = None


class EPSRevision(BaseModel):
    """EPS revision momentum for a single period."""

    period: Optional[str] = None
    up_last_7d: int = 0
    up_last_30d: int = 0
    down_last_7d: int = 0
    down_last_30d: int = 0


class ForwardEstimatesResponse(BaseModel):
    """Response from _fetch_forward_estimates (embedded in get_earnings_history)."""

    forward_estimates: list[ForwardEstimate] = Field(default_factory=list)
    eps_revisions: list[EPSRevision] = Field(default_factory=list)


class AnalystPositioning(BaseModel):
    recommendation_key: Optional[str] = None
    consensus_score: int = 0
    n_analysts: Optional[int] = Field(None, ge=0)
    analyst_target_price: Optional[float] = Field(None, gt=0)
    analyst_upside_pct: Optional[float] = None
    short_ratio: Optional[float] = Field(None, ge=0)
    short_pct_float: Optional[float] = Field(None, ge=0.0, le=1.0)
    earnings_surprise_est: Optional[float] = None
    short_squeeze_risk: bool = False
    recent_upgrades: int = Field(0, ge=0)
    recent_downgrades: int = Field(0, ge=0)
    recent_initiations: int = Field(0, ge=0)
    grade_momentum: Optional[str] = None
    latest_actions: list[AnalystAction] = Field(default_factory=list)
    eps_revision_up_30d: Optional[int] = Field(None, ge=0)
    eps_revision_down_30d: Optional[int] = Field(None, ge=0)
    eps_revision_momentum: Optional[str] = None
    forward_eps_growth: Optional[float] = None


class QuantAgentOutput(BaseModel):
    """Complete validated output of the Quant Analysis Agent (LangGraph)."""

    ticker: str
    recommendation: str = "HOLD"
    reasoning: str = ""
    metrics: QuantRiskMetrics = Field(default_factory=QuantRiskMetrics)  # type: ignore[arg-type]
    dcf_valuation: Optional[DCFValuation] = None
    dcf_error: Optional[str] = None
    stress_test: Optional[StressTestResult] = None
    monte_carlo: Optional[MonteCarloResult] = None
    correlation_matrix: dict[str, Any] = Field(default_factory=dict)
    fundamentals: Optional[Fundamentals] = None
    technicals: Optional[TechnicalIndicators] = None
    peer_comparison: Optional[PeerComparison] = None
    options_signals: Optional[OptionsSignals] = None
    insider_signals: Optional[InsiderSignals] = None
    positioning: Optional[AnalystPositioning] = None
    schema_validation: Optional[dict[str, Any]] = None


# ── RAG Agent model ──────────────────────────────────────────────────────────


class RAGAgentOutput(BaseModel):
    """Validated output of the Financial RAG Agent (LlamaIndex)."""

    ticker: str
    summary: str = ""
    sources: list[Any] = Field(default_factory=list)
    relevance_scores: list[float] = Field(default_factory=list)
    context_texts: list[str] = Field(default_factory=list)
    confidence_score: float = Field(0.0, ge=0.0, le=1.0)


# ── Market Context Agent models ──────────────────────────────────────────────


class MarketContextPeer(BaseModel):
    ticker: str
    metrics: dict[str, str] = Field(default_factory=dict)


class MarketContextOutput(BaseModel):
    """Validated output of the Market Context Agent (CrewAI)."""

    narrative: str = ""
    overall_signal: str = "neutral"
    confidence_score: float = Field(0.0, ge=0.0, le=1.0)
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
    trend_strength: float = Field(0.0, ge=0.0, le=1.0)
    supporting_indicators: list[str] = Field(default_factory=list)


class ForecastResult(BaseModel):
    method: str = "exponential_smoothing"
    horizon_days: int = Field(30, gt=0)
    forecast_prices: list[float] = Field(default_factory=list)
    forecast_dates: list[str] = Field(default_factory=list)
    confidence_lower: list[float] = Field(default_factory=list)
    confidence_upper: list[float] = Field(default_factory=list)
    mape: Optional[float] = Field(None, ge=0)


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
    anomaly_count: int = Field(0, ge=0)
    severity: str = "none"
    catalyst_context: list[str] = Field(default_factory=list)


class AnalyticsAgentOutput(BaseModel):
    ticker: str
    trend_analysis: Optional[TrendAnalysis] = None
    forecast: Optional[ForecastResult] = None
    charts: list[ChartPayload] = Field(default_factory=list)
    statistical_summary: Optional[StatisticalSummary] = None
    anomalies: Optional[AnomalyReport] = None
    analytics_confidence: float = Field(0.0, ge=0.0, le=1.0)
    analytics_signal: str = "neutral"


# ── Reviewer Agent models ───────────────────────────────────────────────────


class ContradictionFlag(BaseModel):
    agents: list[str]
    field: str
    description: str
    severity: str = "low"


class SourceVerification(BaseModel):
    agent_name: str
    claims_checked: int = Field(0, ge=0)
    claims_verified: int = Field(0, ge=0)
    verification_rate: float = Field(0.0, ge=0.0, le=1.0)
    unverified_claims: list[str] = Field(default_factory=list)

    @field_validator("verification_rate", mode="before")
    @classmethod
    def normalize_rate(cls, v: Any) -> Any:
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


class AgentScores(BaseModel):
    quant: float = Field(0.0, ge=0.0, le=1.0)
    rag: float = Field(0.0, ge=0.0, le=1.0)
    market_context: float = Field(0.0, ge=0.0, le=1.0)
    analytics: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("quant", "rag", "market_context", "analytics", mode="before")
    @classmethod
    def normalize_agent_scores(cls, v: Any) -> Any:
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


class ConfidenceBreakdown(BaseModel):
    agent_scores: AgentScores = Field(default_factory=AgentScores)  # type: ignore[arg-type]
    agreement_score: float = Field(0.0, ge=0.0, le=1.0)
    data_quality_score: float = Field(0.0, ge=0.0, le=1.0)
    meta_confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("agreement_score", "data_quality_score", "meta_confidence", mode="before")
    @classmethod
    def normalize_confidence_scores(cls, v: Any) -> Any:
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


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
    review_confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("review_confidence", mode="before")
    @classmethod
    def normalize_review_confidence(cls, v: Any) -> Any:
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


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
