"""Tests for the agent outputs extraction path (Phase 1-2).

Validates that _populate_from_agent_outputs correctly maps structured
agent dicts into DeckData fields, and that _extract_deck_data routes
to the new path when agent keys are present.
"""

import pytest

pytest.importorskip("yfinance")

from unittest.mock import MagicMock, patch  # noqa: E402


def _make_yf_mock(long_name, sector, exchange):
    mock = MagicMock()
    mock.return_value.info = {"longName": long_name, "sector": sector, "exchange": exchange}
    return mock


# ── Realistic agent output fixtures ──────────────────────────────────────────

QUANT_RESPONSE = {
    "ticker": "NVDA",
    "metrics": {
        "sharpe_ratio": 1.85,
        "annual_volatility": 0.32,
        "beta": 1.42,
        "var_95_daily": -0.028,
    },
    "dcf_valuation": {"fair_value": 142.50},
    "monte_carlo": {"p90": 185.00, "p50": 138.00, "p10": 89.00},
    "fundamentals": {
        "pe_ratio": 62.5,
        "roe": 0.935,
        "operating_margin": 0.628,
        "revenue_growth": 1.22,
        "current_ratio": 4.17,
    },
    "technicals": {"rsi": 58.0, "macd_signal": "Bullish"},
    "peer_comparison": {
        "peers": ["NVDA", "AMD", "INTC"],
        "comparison": {
            "NVDA": {
                "pe": 62.5,
                "ev_ebitda": 45.2,
                "rev_growth": 1.22,
                "op_margin": 0.628,
                "roe": 0.935,
            },  # noqa: E501
            "AMD": {
                "pe": 38.1,
                "ev_ebitda": 28.5,
                "rev_growth": 0.18,
                "op_margin": 0.225,
                "roe": 0.045,
            },  # noqa: E501
            "INTC": {
                "pe": 15.2,
                "ev_ebitda": 8.1,
                "rev_growth": -0.12,
                "op_margin": 0.05,
                "roe": -0.02,
            },  # noqa: E501
        },
    },
    "stress_test": {"cvar_95": -4.2, "max_drawdown": -35.0},
    "recommendation": "BUY",
    "reasoning": (
        "NVIDIA dominates AI GPU market with 80%+ share. Strong fundamentals and growth trajectory."
    ),
}

RAG_RESPONSE = {
    "ticker": "NVDA",
    "summary": (
        "NVIDIA reported record Q1 revenue of $44.1B, up 69% YoY, "
        "driven by data center AI demand. The company holds dominant "
        "positioning in AI accelerators with the H100 and upcoming "
        "Blackwell architecture."
    ),
    "sources": [
        {"title": "NVIDIA Q1 2026 10-K Filing", "url": "https://sec.gov/..."},
        {"title": "NVIDIA Investor Relations", "url": "https://ir.nvidia.com"},
    ],
    "confidence_score": 0.92,
    "context_texts": ["NVIDIA reported record revenue...", "Data center segment grew 427%..."],
}

SENTIMENT_RESPONSE = {
    "narrative": (
        "Market sentiment on NVIDIA is overwhelmingly bullish, "
        "driven by AI infrastructure spending. Institutional "
        "ownership is increasing."
    ),
    "overall_signal": "Bullish",
    "confidence_score": 0.85,
    "key_tailwinds": [
        "Explosive AI infrastructure demand from hyperscalers",
        "Blackwell architecture launch driving upgrade cycle",
        "Automotive and robotics expansion",
    ],
    "key_headwinds": [
        "Valuation premium vs historical averages",
        "Geopolitical risks with China export restrictions",
        "Customer concentration in top 4 hyperscalers",
    ],
    "peer_comparison": [
        {"ticker": "AMD", "metrics": {"pe": 38.1, "market_cap": "$250B"}},
        {"ticker": "INTC", "metrics": {"pe": 15.2, "market_cap": "$120B"}},
    ],
}


# ── Tests for _populate_from_agent_outputs ───────────────────────────────────


def test_populate_from_quant_kpi_chips():
    """Quant response populates KPI chips from metrics."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"quant_response": QUANT_RESPONSE}, "")

    labels = [c["label"] for c in data.kpi_chips]
    assert "Sharpe Ratio" in labels
    assert "Annual Volatility" in labels
    assert "Beta" in labels
    assert "RSI" in labels


def test_populate_from_quant_financials():
    """Quant response populates financials table."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"quant_response": QUANT_RESPONSE}, "")

    fin_labels = [f[0] for f in data.financials]
    assert "Sharpe Ratio" in fin_labels
    assert "Volatility" in fin_labels
    assert "Beta" in fin_labels
    assert "VaR (95%, daily)" in fin_labels
    assert "P/E Ratio" in fin_labels
    assert "ROE" in fin_labels


def test_populate_from_quant_valuation():
    """Quant response populates valuation table and scenarios."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"quant_response": QUANT_RESPONSE}, "")

    val_labels = [v[0] for v in data.valuation_table]
    assert "DCF Fair Value" in val_labels
    assert "95th Percentile Target" in val_labels
    assert "Median Target (p50)" in val_labels
    assert "5th Percentile Target" in val_labels
    assert "CVaR (95%)" in val_labels
    assert "Max Drawdown" in val_labels
    assert data.scenarios["dcf"] == "$142.50"
    assert data.scenarios["bull"] == "$185.00"


def test_populate_from_quant_scorecard():
    """Quant response populates scorecard entries."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"quant_response": QUANT_RESPONSE}, "")

    dims = [s[0] for s in data.scorecard]
    assert "Momentum" in dims
    assert "Technical Outlook" in dims
    assert "Quant Signal" in dims
    assert "Recommendation" in dims


def test_populate_from_quant_peers():
    """Quant response populates peer comparison data."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"quant_response": QUANT_RESPONSE}, "")

    assert "AMD" in data.peer_names
    assert "INTC" in data.peer_names
    assert len(data.peers) > 0
    peer_metrics = [p["metric"] for p in data.peers]
    assert "P/E Ratio" in peer_metrics


def test_populate_from_rag_summary():
    """RAG response populates executive summary and sources."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"rag_response": RAG_RESPONSE}, "")

    assert "record Q1 revenue" in data.executive_summary
    assert len(data.sections) >= 1
    assert any(s.title == "Cited Sources" for s in data.sections)


def test_populate_from_sentiment_risks_opportunities():
    """Sentiment response populates risks and opportunities."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"sentiment_response": SENTIMENT_RESPONSE}, "")

    assert len(data.opportunities) >= 2
    assert len(data.risks) >= 2
    assert data.opportunities_extracted is True
    assert data.risks_extracted is True
    assert any("AI" in o for o in data.opportunities)
    assert any("valuation" in r.lower() or "geopolitical" in r.lower() for r in data.risks)


def test_populate_from_sentiment_peers():
    """Sentiment response populates peer comparison when quant hasn't."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    _populate_from_agent_outputs(data, {"sentiment_response": SENTIMENT_RESPONSE}, "")

    assert "AMD" in data.peer_names
    assert any(s[0] == "Market Sentiment" for s in data.scorecard)


def test_populate_all_three_agents():
    """All three agent responses combined populate a rich DeckData."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    brief_data = {
        "quant_response": QUANT_RESPONSE,
        "rag_response": RAG_RESPONSE,
        "sentiment_response": SENTIMENT_RESPONSE,
    }
    _populate_from_agent_outputs(data, brief_data, "")

    assert len(data.kpi_chips) >= 4
    assert len(data.financials) >= 5
    assert len(data.valuation_table) >= 4
    assert len(data.scorecard) >= 4
    assert len(data.peers) >= 2
    assert len(data.risks) >= 2
    assert len(data.opportunities) >= 2
    assert "record Q1 revenue" in data.executive_summary


def test_populate_string_values_handled():
    """Agent response values that are strings (not dicts) are handled gracefully."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    brief_data = {
        "quant_response": "not a json dict",
        "rag_response": '{"invalid json',
        "sentiment_response": 42,
    }
    _populate_from_agent_outputs(data, brief_data, "")
    assert data.executive_summary == ""


def test_populate_empty_agent_dicts():
    """Empty agent dicts don't crash and leave DeckData minimal."""
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _populate_from_agent_outputs

    data = DeckData(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        exchange="NASDAQ",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2026-06-12",
        executive_summary="",
    )
    brief_data = {
        "quant_response": {},
        "rag_response": {},
        "sentiment_response": {},
    }
    _populate_from_agent_outputs(data, brief_data, "")
    assert len(data.kpi_chips) == 0
    assert len(data.financials) == 0


# ── Tests for _extract_deck_data routing ─────────────────────────────────────


def test_extract_deck_data_routes_to_agent_path():
    """_extract_deck_data routes to agent outputs path when agent keys present."""
    import shared.reports.extraction as ext

    brief = {
        "response_text": "Some fallback text",
        "quant_response": QUANT_RESPONSE,
        "rag_response": RAG_RESPONSE,
        "sentiment_response": SENTIMENT_RESPONSE,
    }
    with (
        patch("yfinance.Ticker", _make_yf_mock("NVIDIA Corporation", "Technology", "NMS")),
        patch.object(ext, "_REPORTS_OFFLINE", False),
    ):
        ext._ticker_cache.clear()
        deck = ext._extract_deck_data(brief, "NVDA", "BUY", 0.85, "2026-06-12")

    assert len(deck.kpi_chips) >= 4
    assert len(deck.financials) >= 5
    assert "AMD" in deck.peer_names


def test_extract_deck_data_falls_through_without_agent_keys():
    """Without agent keys, _extract_deck_data uses prose path."""
    import shared.reports.extraction as ext

    brief = {
        "response_text": (
            "Revenue growth: +7.3% YoY. ROE of 24.1%.\n"
            "## Key Risks\n- Valuation risk\n- Competition from AMD\n"
        ),
    }
    with (
        patch("yfinance.Ticker", _make_yf_mock("Walmart Inc.", "Consumer Defensive", "NYQ")),
        patch.object(ext, "_REPORTS_OFFLINE", False),
    ):
        ext._ticker_cache.clear()
        deck = ext._extract_deck_data(brief, "WMT", "HOLD", 0.58, "2026-06-12")

    assert len(deck.kpi_chips) >= 1
    assert len(deck.risks) >= 1


def test_extract_deck_data_agent_path_with_partial_agents():
    """Only 2 of 3 agents → partial structured data, no crash."""
    import shared.reports.extraction as ext

    brief = {
        "response_text": "Fallback text with revenue growth: +10% YoY.",
        "quant_response": QUANT_RESPONSE,
    }
    with (
        patch("yfinance.Ticker", _make_yf_mock("NVIDIA Corporation", "Technology", "NMS")),
        patch.object(ext, "_REPORTS_OFFLINE", False),
    ):
        ext._ticker_cache.clear()
        deck = ext._extract_deck_data(brief, "NVDA", "BUY", 0.85, "2026-06-12")

    assert len(deck.kpi_chips) >= 3
    assert len(deck.valuation_table) >= 4


def test_agent_path_generates_valid_pptx(tmp_path):
    """Agent outputs → PPTX generation works end-to-end."""
    import zipfile
    from io import BytesIO

    from shared.reports import generate_pptx

    brief = {
        "quant_response": QUANT_RESPONSE,
        "rag_response": RAG_RESPONSE,
        "sentiment_response": SENTIMENT_RESPONSE,
    }
    with patch("yfinance.Ticker", _make_yf_mock("NVIDIA Corporation", "Technology", "NMS")):
        buf = generate_pptx(brief, "NVDA", "BUY", 0.85, "2026-06-12")

    assert isinstance(buf, BytesIO)
    data = buf.read()
    assert len(data) > 5000
    with zipfile.ZipFile(BytesIO(data)) as z:
        slide_files = [
            n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
    assert len(slide_files) >= 6


def test_agent_path_generates_valid_html():
    """Agent outputs → HTML generation works end-to-end."""
    from shared.reports import generate_html

    brief = {
        "quant_response": QUANT_RESPONSE,
        "rag_response": RAG_RESPONSE,
        "sentiment_response": SENTIMENT_RESPONSE,
    }
    with patch("yfinance.Ticker", _make_yf_mock("NVIDIA Corporation", "Technology", "NMS")):
        html = generate_html(brief, "NVDA", "BUY", 0.85, "2026-06-12")

    assert isinstance(html, str)
    assert 'class="hero"' in html
    assert "NVDA" in html
