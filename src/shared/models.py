"""Pydantic data models for the multi-agent investment pipeline.

Each model represents data flowing between agents/stages:
  QueryContext       → orchestrator input (user request)
  RAGInsights        → RAG agent → orchestrator
  QuantMetrics       → quant agent → orchestrator
  MarketContext → market context agent → orchestrator
  InvestmentBrief    → orchestrator → final output (user)
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── QueryContext: raw user request that enters the orchestrator ──
class QueryContext(BaseModel):
    ticker: str
    user_query: str
    user_risk_profile: str
    portfolio_holdings: list[str]
    investment_horizon: str
    session_id: str
    user_id: str | None = None
    timestamp: datetime


# ── Agent output models — canonical definitions live in agent_models.py ──
# Re-exported here for backward compatibility with existing imports.
from shared.agent_models import (  # noqa: E402
    MarketContextOutput as MarketContext,
)
from shared.agent_models import (  # noqa: E402
    QuantAgentOutput as QuantMetrics,
)
from shared.agent_models import (  # noqa: E402
    RAGAgentOutput as RAGInsights,
)


# ── InvestmentBrief: final aggregation → output to user ──
class InvestmentBrief(BaseModel):
    ticker: str
    generated_at: datetime
    query_context: QueryContext
    rag_insights: RAGInsights
    quant_metrics: QuantMetrics
    market_context: MarketContext
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
