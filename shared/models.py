from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class QueryContext(BaseModel):
    ticker: str
    user_query: str
    user_risk_profile: str
    portfolio_holdings: list[str]
    investment_horizon: str
    session_id: str
    timestamp: datetime


class RAGInsights(BaseModel):
    ticker: str
    revenue_growth_yoy: float
    rd_spend_billions: float
    forward_guidance: str
    key_risks: list[str]
    cited_documents: list[str]
    confidence_score: float


class QuantMetrics(BaseModel):
    ticker: str
    sharpe_ratio: float
    annual_volatility: float
    beta: float
    var_95_daily: float
    dcf_intrinsic_value: Optional[float] = None
    stress_test_result: Optional[dict] = None
    portfolio_correlation: dict
    quant_signal: str
    quant_confidence: float


class SentimentIntelligence(BaseModel):
    ticker: str
    social_sentiment_score: float
    analyst_consensus: str
    avg_price_target: float
    insider_signal: str
    narrative: str
    overall_signal: str
    confidence_score: float
    key_risks: list[str]
    key_catalysts: list[str]


class InvestmentBrief(BaseModel):
    ticker: str
    generated_at: datetime
    query_context: QueryContext
    rag_insights: RAGInsights
    quant_metrics: QuantMetrics
    sentiment_intelligence: SentimentIntelligence
    final_recommendation: str
    recommendation_rationale: str
    confidence_score: float
    disclaimer: str
