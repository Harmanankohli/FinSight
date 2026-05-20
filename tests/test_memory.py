"""Tests for the FinSight memory layer."""

import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

os.environ["AGENT_SEED_URLS"] = ""

from shared.memory.store import get_db, init_db
from shared.memory.ticker_memory import TickerMemory
from shared.memory.portfolio_store import PortfolioStore
from shared.memory.performance_tracker import PerformanceTracker
from shared.memory.memory_service import SQLiteMemoryService
from shared.models import InvestmentBrief, QueryContext, RAGInsights, QuantMetrics, SentimentIntelligence


@pytest.fixture
def db_path():
    """Create a temporary database for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)
    path.with_suffix(".db-wal").unlink(missing_ok=True)
    path.with_suffix(".db-shm").unlink(missing_ok=True)


@pytest.fixture
async def db_connection(db_path):
    """Create and initialize a database connection."""
    conn = await get_db(db_path)
    await init_db(conn)
    yield conn
    await conn.close()


@pytest.fixture
def sample_brief():
    """Create a sample InvestmentBrief for testing."""
    return InvestmentBrief(
        ticker="NVDA",
        generated_at=datetime.utcnow(),
        query_context=QueryContext(
            ticker="NVDA",
            user_query="Should I invest in NVDA?",
            user_risk_profile="aggressive",
            portfolio_holdings=["AAPL", "MSFT"],
            investment_horizon="long_term",
            session_id="test_session",
            timestamp=datetime.utcnow(),
        ),
        rag_insights=RAGInsights(
            ticker="NVDA",
            revenue_growth_yoy=0.26,
            rd_spend_billions=8.5,
            forward_guidance="Strong data center demand expected to continue",
            key_risks=["Supply chain constraints", "Competition from AMD"],
            cited_documents=["NVDA_10K_2024.pdf"],
            confidence_score=0.85,
        ),
        quant_metrics=QuantMetrics(
            ticker="NVDA",
            sharpe_ratio=1.8,
            annual_volatility=0.35,
            beta=1.6,
            var_95_daily=-0.025,
            dcf_intrinsic_value=145.0,
            portfolio_correlation={"AAPL": 0.6, "MSFT": 0.55},
            quant_signal="BUY",
            quant_confidence=0.78,
        ),
        sentiment_intelligence=SentimentIntelligence(
            ticker="NVDA",
            social_sentiment_score=0.72,
            analyst_consensus="Strong Buy",
            avg_price_target=155.0,
            insider_signal="Neutral",
            narrative="AI chip leader with strong momentum",
            overall_signal="BUY",
            confidence_score=0.80,
            key_risks=["Valuation stretched", "Regulatory risks"],
            key_catalysts=["New Blackwell architecture", "Data center growth"],
        ),
        final_recommendation="BUY",
        recommendation_rationale="Strong AI tailwinds, data center demand accelerating, valuation justified by growth",
        confidence_score=0.78,
        disclaimer="This is not financial advice.",
    )


class TestStore:
    """Tests for the SQLite foundation."""

    async def test_init_db_creates_tables(self, db_connection):
        """Verify all tables are created."""
        cursor = await db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        assert "ticker_briefs" in tables
        assert "user_profiles" in tables
        assert "recommendation_records" in tables
        assert "memory_entries" in tables
        assert "schema_version" in tables

    async def test_init_db_is_idempotent(self, db_connection):
        """Running init_db twice should not fail."""
        await init_db(db_connection)
        cursor = await db_connection.execute("SELECT version FROM schema_version")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1


class TestTickerMemory:
    """Tests for per-ticker recommendation history."""

    async def test_store_and_get_latest(self, db_path, sample_brief):
        tm = TickerMemory(db_path)
        record_id = await tm.store_brief(sample_brief, "test_user", "session_1")
        assert record_id is not None

        latest = await tm.get_latest("NVDA")
        assert latest is not None
        assert latest["ticker"] == "NVDA"
        assert latest["recommendation"] == "BUY"
        assert latest["confidence"] == 0.78

    async def test_store_minimal(self, db_path):
        tm = TickerMemory(db_path)
        record_id = await tm.store_minimal(
            ticker="AAPL",
            user_id="test_user",
            session_id="session_1",
            query="Analyze AAPL",
            response_text="AAPL looks strong",
            recommendation="BUY",
            confidence=0.7,
        )
        assert record_id is not None

        latest = await tm.get_latest("AAPL")
        assert latest["recommendation"] == "BUY"

    async def test_get_history(self, db_path, sample_brief):
        tm = TickerMemory(db_path)
        await tm.store_brief(sample_brief, "test_user", "session_1")

        import time
        time.sleep(0.01)

        brief2 = InvestmentBrief.model_validate(sample_brief.model_dump())
        brief2.confidence_score = 0.82
        brief2.generated_at = datetime.utcnow()
        await tm.store_brief(brief2, "test_user", "session_2")

        history = await tm.get_history("NVDA", limit=5)
        assert len(history) == 2
        assert history[0]["confidence"] == 0.82
        assert history[1]["confidence"] == 0.78

    async def test_has_changed(self, db_path, sample_brief):
        tm = TickerMemory(db_path)
        await tm.store_brief(sample_brief, "test_user", "session_1")

        import time
        time.sleep(0.01)

        brief2 = InvestmentBrief.model_validate(sample_brief.model_dump())
        brief2.final_recommendation = "HOLD"
        brief2.generated_at = datetime.utcnow()
        await tm.store_brief(brief2, "test_user", "session_2")

        change = await tm.has_changed("NVDA", "recommendation")
        assert change is not None
        assert change["changed"] is True
        assert change["old"] == "HOLD"
        assert change["new"] == "BUY"

    async def test_format_context_empty(self, db_path):
        tm = TickerMemory(db_path)
        context = await tm.format_context("UNKNOWN")
        assert context == ""

    async def test_format_context_with_data(self, db_path, sample_brief):
        tm = TickerMemory(db_path)
        await tm.store_brief(sample_brief, "test_user", "session_1")

        context = await tm.format_context("NVDA", max_tokens=300)
        assert "NVDA" in context
        assert "BUY" in context
        assert "0.78" in context


class TestPortfolioStore:
    """Tests for user portfolio persistence."""

    async def test_upsert_from_context_creates_profile(self, db_path):
        ps = PortfolioStore(db_path)
        ctx = QueryContext(
            ticker="NVDA",
            user_query="Should I buy NVDA?",
            user_risk_profile="aggressive",
            portfolio_holdings=["AAPL", "MSFT"],
            investment_horizon="long_term",
            session_id="user_123",
            timestamp=datetime.utcnow(),
        )
        await ps.upsert_from_context(ctx)

        profile = await ps.get_profile("user_123")
        assert profile is not None
        assert profile["risk_profile"] == "aggressive"
        assert set(profile["holdings"]) == {"AAPL", "MSFT"}
        assert profile["horizon"] == "long_term"

    async def test_upsert_merges_holdings(self, db_path):
        ps = PortfolioStore(db_path)
        ctx1 = QueryContext(
            ticker="NVDA",
            user_query="Test",
            user_risk_profile="medium",
            portfolio_holdings=["AAPL"],
            investment_horizon="medium_term",
            session_id="user_456",
            timestamp=datetime.utcnow(),
        )
        await ps.upsert_from_context(ctx1)

        ctx2 = QueryContext(
            ticker="GOOGL",
            user_query="Test",
            user_risk_profile="medium",
            portfolio_holdings=["GOOGL", "MSFT"],
            investment_horizon="medium_term",
            session_id="user_456",
            timestamp=datetime.utcnow(),
        )
        await ps.upsert_from_context(ctx2)

        profile = await ps.get_profile("user_456")
        assert set(profile["holdings"]) == {"AAPL", "GOOGL", "MSFT"}

    async def test_get_holdings_empty(self, db_path):
        ps = PortfolioStore(db_path)
        holdings = await ps.get_holdings("nonexistent")
        assert holdings == []

    async def test_update_holdings(self, db_path):
        ps = PortfolioStore(db_path)
        await ps.update_holdings("user_789", ["TSLA", "AMZN"])
        holdings = await ps.get_holdings("user_789")
        assert set(holdings) == {"TSLA", "AMZN"}


class TestPerformanceTracker:
    """Tests for recommendation outcome tracking."""

    async def test_record_recommendation(self, db_path):
        pt = PerformanceTracker(db_path)
        record_id = await pt.record_recommendation(
            ticker="NVDA",
            user_id="test_user",
            recommendation="BUY",
            confidence=0.8,
            price=120.0,
        )
        assert record_id is not None

        recs = await pt.get_past_recommendations("NVDA", days=30)
        assert len(recs) == 1
        assert recs[0]["recommendation"] == "BUY"
        assert recs[0]["price_at_rec"] == 120.0

    async def test_get_past_recommendations_empty(self, db_path):
        pt = PerformanceTracker(db_path)
        recs = await pt.get_past_recommendations("UNKNOWN", days=7)
        assert recs == []

    async def test_fetch_current_price_returns_none_for_invalid(self, db_path):
        """yfinance should return None for an invalid ticker."""
        price = PerformanceTracker._fetch_current_price("INVALIDTICKER123")
        assert price is None


class TestSQLiteMemoryService:
    """Tests for the persistent memory service."""

    async def test_search_memory_empty(self, db_path):
        ms = SQLiteMemoryService(db_path)
        result = await ms.search_memory(
            app_name="test_app",
            user_id="test_user",
            query="anything",
        )
        assert len(result.memories) == 0

    async def test_add_events_and_search(self, db_path):
        """Events added via add_events_to_memory should be searchable."""
        from google.adk.events import Event
        from google.genai import types

        ms = SQLiteMemoryService(db_path)

        user_event = Event(
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Should I invest in NVDA?")],
            ),
        )
        agent_event = Event(
            author="model",
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="I recommend you invest in NVDA. BUY with confidence 0.8")],
            ),
        )

        await ms.add_events_to_memory(
            app_name="test_app",
            user_id="test_user",
            events=[user_event, agent_event],
            session_id="session_123",
        )

        result = await ms.search_memory(
            app_name="test_app",
            user_id="test_user",
            query="NVDA invest",
        )
        assert len(result.memories) == 2

    async def test_bm25_ranks_relevant_higher(self, db_path):
        """BM25 should rank more relevant results higher."""
        from google.adk.events import Event
        from google.genai import types

        ms = SQLiteMemoryService(db_path)

        # Unrelated conversation
        unrelated = Event(
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part.from_text(text="What is the weather today?")],
            ),
        )
        # Highly relevant
        relevant = Event(
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Should I invest in NVDA? What is the DCF valuation for NVIDIA?")],
            ),
        )
        # Somewhat relevant
        somewhat = Event(
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part.from_text(text="Tell me about the stock market trends")],
            ),
        )

        await ms.add_events_to_memory(
            app_name="test_app",
            user_id="test_user",
            events=[unrelated, relevant, somewhat],
            session_id="session_123",
        )

        result = await ms.search_memory(
            app_name="test_app",
            user_id="test_user",
            query="NVDA invest",
        )
        assert len(result.memories) >= 1
        # Most relevant should be first
        assert "NVDA" in result.memories[0].content.parts[0].text

    async def test_add_session_to_memory_and_search(self, db_path):
        """Standard ADK pattern: add_session_to_memory(session) should work."""
        from google.adk.events import Event
        from google.adk.sessions import Session
        from google.genai import types

        ms = SQLiteMemoryService(db_path)

        session = Session(
            id="session_456",
            app_name="test_app",
            user_id="test_user",
            state={},
            events=[
                Event(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[types.Part.from_text(text="What is the DCF for TSLA?")],
                    ),
                ),
                Event(
                    author="model",
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="TSLA DCF shows 20% upside. BUY recommendation.")],
                    ),
                ),
            ],
        )

        await ms.add_session_to_memory(session)

        result = await ms.search_memory(
            app_name="test_app",
            user_id="test_user",
            query="TSLA DCF valuation",
        )
        assert len(result.memories) == 2
        # Most relevant should be first
        assert "TSLA" in result.memories[0].content.parts[0].text
