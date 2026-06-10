"""Pydantic data models for the multi-agent investment pipeline.

Each model represents data flowing between agents/stages:
  QueryContext       → orchestrator input (user request)
  RAGInsights        → RAG agent → orchestrator
  QuantMetrics       → quant agent → orchestrator
  SentimentIntelligence → sentiment agent → orchestrator
  InvestmentBrief    → orchestrator → final output (user)
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


# ── QueryContext: raw user request that enters the orchestrator ──
class QueryContext(BaseModel):
    ticker: str
    user_query: str
    user_risk_profile: str
    portfolio_holdings: list[str]
    investment_horizon: str
    session_id: str
    timestamp: datetime


# ── RAGInsights: structured filing / earnings insight from RAG agent ──
class RAGInsights(BaseModel):
    ticker: str
    revenue_growth_yoy: float
    rd_spend_billions: float
    forward_guidance: str
    key_risks: list[str]
    cited_documents: list[str]
    confidence_score: float


# ── QuantMetrics: computed financial risk/valuation from quant agent ──
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


# ── SentimentIntelligence: market sentiment from sentiment agent ──
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
    peer_comparison: list[dict] = []


# ── InvestmentBrief: final aggregation → output to user ──
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


# ── API Response Models (WP 3.2) — used by FastAPI sub-app for OpenAPI spec ──


class HealthResponse(BaseModel):
    status: str
    agent: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class MemoryTickerItem(BaseModel):
    id: int
    ticker: str
    recommendation: str
    confidence: float
    analysis_date: str
    created_at: str


class MemoryTickerChangedResponse(BaseModel):
    changed: bool
    reason: str | None = None
    old: str | None = None
    new: str | None = None


class SessionListItem(BaseModel):
    session_id: str
    user_id: str
    created_at: str
    event_count: int


class SessionEventsResponse(BaseModel):
    session_id: str
    events: list[dict[str, Any]]


class AgentListItem(BaseModel):
    name: str
    description: str
    skills: list[dict[str, str]] = []


class AgentHealthResponse(BaseModel):
    status: str
    detail: dict[str, Any] | None = None
    error: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    id: str
    username: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str
    user: UserInfo


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str


class MeResponse(BaseModel):
    id: str
    username: str
    role: str
    disabled: int


class LogoutResponse(BaseModel):
    status: str
