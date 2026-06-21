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
    quant_confidence: float | None = Field(None, ge=0.0, le=1.0)
    quant_signal: str | None = None
    signals: list[str] = Field(default_factory=list)
    signal_scores: dict[str, float] = Field(default_factory=dict)


class DCFValuation(BaseModel):
    intrinsic_value: float = Field(gt=0)
    current_price: float = Field(gt=0)
    upside_pct: float
    method: str = "dcf"
    wacc: float | None = Field(None, ge=0.0, le=1.0)
    growth_rate: float | None = None
    terminal_growth: float | None = Field(None, ge=0.0, le=0.03)
    enterprise_value: float | None = Field(None, gt=0)
    market_enterprise_value: float | None = Field(None, gt=0)
    net_debt: float | None = None
    equity_value: float | None = Field(None, gt=0)
    fcf_used: float | None = Field(None, gt=0)
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    dcf_assumptions: dict | None = None
    fcf_age_days: float | None = Field(None, ge=0)
    freshness: dict | None = None
    valuation_warnings: list | None = None
    scenarios: dict | None = None
    valuation_reliability: float | None = Field(None, ge=0.0, le=1.0)
    pb_ratio_used: float | None = Field(None, gt=0)
    fair_pb_multiple: float | None = Field(None, gt=0)
    sensitivity_matrix: dict | None = None


class MonteCarloResult(BaseModel):
    p10: float = Field(gt=0)
    p25: float | None = Field(None, gt=0)
    p50: float = Field(gt=0)
    p75: float | None = Field(None, gt=0)
    p90: float = Field(gt=0)
    prob_profit: float = Field(ge=0.0, le=1.0)
    expected_return_pct: float | None = None
    mc_var_95: float | None = Field(None, le=0)
    current_price: float | None = Field(None, gt=0)
    n_simulations: int | None = Field(None, gt=0)
    horizon_days: int | None = Field(None, gt=0)


class StressScenario(BaseModel):
    market_decline_pct: float | None = Field(None, le=0)
    beta_adj_decline_pct: float | None = Field(None, le=0)
    projected_price: float | None = Field(None, gt=0)
    loss_per_share: float | None = Field(None, le=0)


class StressTestResult(BaseModel):
    scenarios: dict[str, StressScenario] = Field(default_factory=dict)
    var_95: float | None = Field(None, le=0)
    cvar_95: float | None = Field(None, le=0)
    beta_used: float | None = Field(None, ge=-3, le=6)
    beta_adjusted: bool = True
    # When stress test was skipped:
    note: str | None = None
    volatility: float | None = Field(None, ge=0)
    threshold: float | None = Field(None, ge=0)


class TechnicalIndicators(BaseModel):
    sma_20: float | None = Field(None, gt=0)
    sma_50: float | None = Field(None, gt=0)
    sma_200: float | None = Field(None, gt=0)
    ema_12: float | None = Field(None, gt=0)
    ema_26: float | None = Field(None, gt=0)
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    macd_bullish: bool | None = None
    rsi: float | None = Field(None, ge=0, le=100)
    rsi_14: float | None = Field(None, ge=0, le=100)
    bb_upper: float | None = Field(None, gt=0)
    bb_lower: float | None = Field(None, gt=0)
    bb_position: float | None = Field(None, ge=0.0, le=1.0)
    momentum_20d: float | None = None
    momentum_60d: float | None = None
    support: float | None = Field(None, gt=0)
    resistance: float | None = Field(None, gt=0)
    trend: str | None = None
    golden_cross: bool | None = None
    above_50d_ma: bool | None = None
    above_200d_ma: bool | None = None

    @property
    def rsi_value(self) -> float | None:
        """Return rsi_14 if set, fall back to rsi."""
        return self.rsi_14 if self.rsi_14 is not None else self.rsi


class Fundamentals(BaseModel):
    model_config = {"populate_by_name": True}

    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = Field(None, gt=0)
    price_to_sales: float | None = Field(None, gt=0)
    ev_to_ebitda: float | None = None
    ev_to_revenue: float | None = Field(None, gt=0)
    gross_margin: float | None = Field(None, ge=-1.0, le=1.0)
    operating_margin: float | None = Field(None, ge=-1.0, le=1.0)
    profit_margin: float | None = Field(None, ge=-1.0, le=1.0)
    roe: float | None = None
    roa: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    earnings_quarterly_growth: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = Field(None, ge=0)
    quick_ratio: float | None = Field(None, ge=0)
    total_debt: float | None = Field(None, ge=0)
    total_cash: float | None = Field(None, ge=0)
    market_cap: float | None = Field(None, gt=0)
    dividend_yield: float | None = Field(None, ge=0.0, le=1.0)
    payout_ratio: float | None = None
    # Digit-prefixed keys — alias matches the actual dict key from quant/nodes/data_fetch.py
    high_52w: float | None = Field(None, alias="52w_high", gt=0)
    low_52w: float | None = Field(None, alias="52w_low", gt=0)
    avg_50d: float | None = Field(None, alias="50d_avg", gt=0)
    avg_200d: float | None = Field(None, alias="200d_avg", gt=0)
    current_price: float | None = Field(None, gt=0)
    pct_from_52w_high: float | None = None
    pct_from_52w_low: float | None = Field(None, ge=0)
    golden_cross: bool | None = None
    net_debt: float | None = None
    sector: str | None = None
    industry: str | None = None


class PeerComparison(BaseModel):
    industry: str | None = None
    sector: str | None = None
    peers: list[str] = Field(default_factory=list)
    comparison: dict[str, dict] = Field(default_factory=dict)
    rankings: dict[str, int] = Field(default_factory=dict)
    n_peers: int = Field(0, ge=0)
    medians: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


class OptionsSignals(BaseModel):
    put_call_volume_ratio: float | None = None
    put_call_oi_ratio: float | None = None
    call_volume: int = Field(0, ge=0)
    put_volume: int = Field(0, ge=0)
    total_volume: int = Field(0, ge=0)
    flow_signal: str = "no_data"
    note: str | None = None


class InsiderSignals(BaseModel):
    recent_transaction_count: int = Field(0, ge=0)
    buy_signals: int = Field(0, ge=0)
    sell_signals: int = Field(0, ge=0)
    direction: str = "neutral"
    net_shares: float | None = None
    net_value: float | None = None
    insider_pct_held: float | None = Field(None, ge=0.0, le=1.0)
    activity_level: str = "low"


class AnalystAction(BaseModel):
    """Single analyst upgrade/downgrade/initiation action."""

    date: str = ""
    firm: str = ""
    action: str = ""
    from_grade: str = ""
    to_grade: str = ""
    prior_target: float | None = None
    new_target: float | None = None
    target_action: str = ""


class AnalystActivityResponse(BaseModel):
    """Response from get_analyst_activity MCP tool."""

    ticker: str
    activities: list[AnalystAction] = Field(default_factory=list)
    total: int = 0
    upgrades: int = 0
    downgrades: int = 0
    initiations: int = 0
    error: str | None = None


class ForwardEstimate(BaseModel):
    """Single forward EPS/revenue estimate period."""

    period: str | None = None
    end_date: str | None = None
    growth: float | None = None
    eps_avg: float | None = None
    eps_low: float | None = None
    eps_high: float | None = None
    eps_year_ago: float | None = None
    n_analysts: int | None = None
    revenue_avg: float | None = None
    revenue_growth: float | None = None


class EPSRevision(BaseModel):
    """EPS revision momentum for a single period."""

    period: str | None = None
    up_last_7d: int = 0
    up_last_30d: int = 0
    down_last_7d: int = 0
    down_last_30d: int = 0


class ForwardEstimatesResponse(BaseModel):
    """Response from _fetch_forward_estimates (embedded in get_earnings_history)."""

    forward_estimates: list[ForwardEstimate] = Field(default_factory=list)
    eps_revisions: list[EPSRevision] = Field(default_factory=list)


class AnalystPositioning(BaseModel):
    recommendation_key: str | None = None
    consensus_score: int = 0
    n_analysts: int | None = Field(None, ge=0)
    analyst_target_price: float | None = Field(None, gt=0)
    analyst_upside_pct: float | None = None
    short_ratio: float | None = Field(None, ge=0)
    short_pct_float: float | None = Field(None, ge=0.0, le=1.0)
    earnings_surprise_est: float | None = None
    short_squeeze_risk: bool = False
    recent_upgrades: int = Field(0, ge=0)
    recent_downgrades: int = Field(0, ge=0)
    recent_initiations: int = Field(0, ge=0)
    grade_momentum: str | None = None
    latest_actions: list[AnalystAction] = Field(default_factory=list)
    eps_revision_up_30d: int | None = Field(None, ge=0)
    eps_revision_down_30d: int | None = Field(None, ge=0)
    eps_revision_momentum: str | None = None
    forward_eps_growth: float | None = None


class QuantAgentOutput(BaseModel):
    """Complete validated output of the Quant Analysis Agent (LangGraph)."""

    ticker: str
    recommendation: str = "HOLD"
    reasoning: str = ""
    metrics: QuantRiskMetrics = Field(default_factory=QuantRiskMetrics)
    dcf_valuation: DCFValuation | None = None
    dcf_error: str | None = None
    stress_test: StressTestResult | None = None
    monte_carlo: MonteCarloResult | None = None
    correlation_matrix: dict = Field(default_factory=dict)
    fundamentals: Fundamentals | None = None
    technicals: TechnicalIndicators | None = None
    peer_comparison: PeerComparison | None = None
    options_signals: OptionsSignals | None = None
    insider_signals: InsiderSignals | None = None
    positioning: AnalystPositioning | None = None
    schema_validation: dict | None = None


# ── RAG Agent model ──────────────────────────────────────────────────────────


class RAGAgentOutput(BaseModel):
    """Validated output of the Financial RAG Agent (LlamaIndex)."""

    ticker: str
    summary: str = ""
    sources: list = Field(default_factory=list)
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
    ma_crossover_signal: str | None = None
    momentum_shift: str | None = None
    trend_strength: float = Field(0.0, ge=0.0, le=1.0)
    supporting_indicators: list[str] = Field(default_factory=list)


class ForecastResult(BaseModel):
    method: str = "exponential_smoothing"
    horizon_days: int = Field(30, gt=0)
    forecast_prices: list[float] = Field(default_factory=list)
    forecast_dates: list[str] = Field(default_factory=list)
    confidence_lower: list[float] = Field(default_factory=list)
    confidence_upper: list[float] = Field(default_factory=list)
    mape: float | None = Field(None, ge=0)


class ChartPayload(BaseModel):
    chart_type: str = "candlestick"
    labels: list[str] = Field(default_factory=list)
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class StatisticalSummary(BaseModel):
    return_distribution: str | None = None
    skewness: float | None = None
    kurtosis: float | None = None
    jarque_bera_pvalue: float | None = None
    correlations: dict[str, float] = Field(default_factory=dict)
    regression_beta: float | None = None
    regression_r_squared: float | None = None


class AnomalyReport(BaseModel):
    price_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    volume_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    fundamental_anomalies: list[str] = Field(default_factory=list)
    anomaly_count: int = Field(0, ge=0)
    severity: str = "none"
    catalyst_context: list[str] = Field(default_factory=list)


class AnalyticsAgentOutput(BaseModel):
    ticker: str
    trend_analysis: TrendAnalysis | None = None
    forecast: ForecastResult | None = None
    charts: list[ChartPayload] = Field(default_factory=list)
    statistical_summary: StatisticalSummary | None = None
    anomalies: AnomalyReport | None = None
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
    def normalize_rate(cls, v):
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
    def normalize_agent_scores(cls, v):
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


class ConfidenceBreakdown(BaseModel):
    agent_scores: AgentScores = Field(default_factory=AgentScores)
    agreement_score: float = Field(0.0, ge=0.0, le=1.0)
    data_quality_score: float = Field(0.0, ge=0.0, le=1.0)
    meta_confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("agreement_score", "data_quality_score", "meta_confidence", mode="before")
    @classmethod
    def normalize_confidence_scores(cls, v):
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
    confidence_breakdown: ConfidenceBreakdown | None = None
    recommendation_validation: RecommendationValidation | None = None
    flags: list[str] = Field(default_factory=list)
    review_confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("review_confidence", mode="before")
    @classmethod
    def normalize_review_confidence(cls, v):
        if isinstance(v, (int, float)) and v > 1.0:
            return v / 100.0
        return v


# ── Combined report-level model ──────────────────────────────────────────────


class ValidatedAgentOutputs(BaseModel):
    """All agent outputs validated together at the orchestrator level."""

    ticker: str
    quant: QuantAgentOutput | None = None
    rag: RAGAgentOutput | None = None
    market_context: MarketContextOutput | None = None
    analytics: AnalyticsAgentOutput | None = None
    reviewer: ReviewerAgentOutput | None = None

    @property
    def has_all_agents(self) -> bool:
        return all([self.quant, self.rag, self.market_context, self.analytics, self.reviewer])
