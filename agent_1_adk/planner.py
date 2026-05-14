import logging
import re

from shared.models import QueryContext
from shared.types import PlannerTask, TaskList

logger = logging.getLogger(__name__)


def extract_ticker(text: str) -> str | None:
    match = re.search(r"\b[A-Z]{2,5}\b", text)
    return match.group(0) if match else None


def extract_risk_profile(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["conservative", "low risk", "safe"]):
        return "conservative"
    if any(w in text_lower for w in ["aggressive", "high risk", "growth"]):
        return "aggressive"
    return "moderate"


def extract_horizon(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["short", "near", "soon"]):
        return "short"
    if any(w in text_lower for w in ["long", "retirement", "distant"]):
        return "long"
    return "medium"


def parse_query(query: str, portfolio: list[str] | None = None) -> QueryContext:
    from datetime import datetime, timezone

    ticker = extract_ticker(query) or ""
    risk = extract_risk_profile(query)
    horizon = extract_horizon(query)

    return QueryContext(
        ticker=ticker,
        user_query=query,
        user_risk_profile=risk,
        portfolio_holdings=portfolio or [],
        investment_horizon=horizon,
        session_id=f"session-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        timestamp=datetime.now(timezone.utc),
    )


def plan(context: QueryContext) -> TaskList:
    """Decompose a QueryContext into an ordered sequence of tasks."""
    ticker = context.ticker
    tasks = [
        PlannerTask(
            id=1,
            description=f"Retrieve SEC filings and financial documents for {ticker}",
            skill_id="sec_filing_retrieval",
            params={"ticker": ticker, "query": context.user_query},
        ),
        PlannerTask(
            id=2,
            description=f"Compute quantitative risk metrics and valuation for {ticker}",
            skill_id="quant_analysis",
            params={"ticker": ticker, "period": "5y"},
        ),
        PlannerTask(
            id=3,
            description=f"Analyze market sentiment and insider signals for {ticker}",
            skill_id="sentiment_analysis",
            params={"ticker": ticker, "query": context.user_query},
        ),
    ]
    return TaskList(original_query=context.user_query, tasks=tasks)
