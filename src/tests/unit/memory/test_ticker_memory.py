"""Unit tests for the TickerMemory persistence layer."""

from datetime import datetime, timedelta, timezone

from shared.memory.ticker_memory import TickerMemory
from shared.models import (
    InvestmentBrief,
    MarketContext,
    QuantMetrics,
    QueryContext,
    RAGInsights,
)

_BASE_TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_brief(ticker="AAPL", rec="BUY", confidence=0.8, offset_hours=0):
    ts = _BASE_TS + timedelta(hours=offset_hours)
    qc = QueryContext(
        ticker=ticker,
        user_query=f"analyze {ticker}",
        user_risk_profile="medium",
        portfolio_holdings=[],
        investment_horizon="medium_term",
        session_id="s1",
        timestamp=ts,
    )
    rag = RAGInsights(
        ticker=ticker,
        revenue_growth_yoy=0.10,
        rd_spend_billions=2.0,
        forward_guidance="positive",
        key_risks=["competition"],
        cited_documents=["10-K"],
        confidence_score=0.9,
    )
    quant = QuantMetrics(
        ticker=ticker,
        sharpe_ratio=1.2,
        annual_volatility=0.22,
        beta=1.0,
        var_95_daily=-0.018,
        portfolio_correlation={},
        quant_signal=rec,
        quant_confidence=confidence,
    )
    market_ctx = MarketContext(
        ticker=ticker,
        narrative="steady",
        overall_signal=rec,
        confidence_score=confidence,
    )
    return InvestmentBrief(
        ticker=ticker,
        generated_at=ts,
        query_context=qc,
        rag_insights=rag,
        quant_metrics=quant,
        market_context=market_ctx,
        final_recommendation=rec,
        recommendation_rationale=f"{ticker} looks {rec.lower()}",
        confidence_score=confidence,
        disclaimer="Not financial advice",
    )


async def test_store_and_get_latest(memory_db):
    mem = TickerMemory(db_path=memory_db)
    brief = _make_brief("AAPL", "BUY")
    await mem.store_brief(brief, user_id="u1", session_id="s1")
    result = await mem.get_latest("AAPL")
    assert result is not None
    assert result["ticker"] == "AAPL"
    assert result["recommendation"] == "BUY"
    assert abs(result["confidence"] - 0.8) < 1e-6


async def test_get_latest_case_insensitive(memory_db):
    mem = TickerMemory(db_path=memory_db)
    brief = _make_brief("NVDA", "HOLD")
    await mem.store_brief(brief, user_id="u1", session_id="s1")
    # get_latest uppercases the ticker
    result = await mem.get_latest("nvda")
    assert result is not None
    assert result["ticker"] == "NVDA"


async def test_get_latest_returns_none_when_empty(memory_db):
    mem = TickerMemory(db_path=memory_db)
    result = await mem.get_latest("TSLA")
    assert result is None


async def test_get_history_returns_all_records(memory_db):
    mem = TickerMemory(db_path=memory_db)
    for i, rec in enumerate(["BUY", "HOLD", "SELL"]):
        brief = _make_brief("MSFT", rec, offset_hours=i)
        await mem.store_brief(brief, user_id="u1", session_id=f"s{i}")
    history = await mem.get_history("MSFT", limit=10)
    assert len(history) == 3
    recs = {h["recommendation"] for h in history}
    assert recs == {"BUY", "HOLD", "SELL"}


async def test_has_changed_detects_flip(memory_db):
    mem = TickerMemory(db_path=memory_db)
    brief1 = _make_brief("GOOG", "BUY", offset_hours=0)
    brief2 = _make_brief("GOOG", "SELL", offset_hours=1)
    await mem.store_brief(brief1, user_id="u1", session_id="s1")
    await mem.store_brief(brief2, user_id="u1", session_id="s2")
    change = await mem.has_changed("GOOG")
    assert change is not None
    assert change["changed"] is True


async def test_has_changed_returns_none_for_single_entry(memory_db):
    mem = TickerMemory(db_path=memory_db)
    brief = _make_brief("AMZN", "HOLD")
    await mem.store_brief(brief, user_id="u1", session_id="s1")
    result = await mem.has_changed("AMZN")
    assert result is None


async def test_store_minimal(memory_db):
    mem = TickerMemory(db_path=memory_db)
    record_id = await mem.store_minimal(
        ticker="META",
        user_id="u1",
        session_id="s1",
        query="analyze META",
        response_text="META is trading at a discount",
        recommendation="BUY",
        confidence=0.65,
    )
    assert record_id is not None
    result = await mem.get_latest("META")
    assert result is not None
    assert result["recommendation"] == "BUY"
