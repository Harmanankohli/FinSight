import json
from pathlib import Path


def _load_card(name):
    path = Path(__file__).resolve().parent.parent / "agent_cards" / f"{name}.json"
    with path.open("r") as f:
        return json.load(f)


def test_orchestrator_card():
    card = _load_card("orchestrator_agent")
    assert card["name"] == "Investment Orchestrator"
    assert len(card["skills"]) == 1
    assert card["skills"][0]["id"] == "investment_research"


def test_rag_card():
    card = _load_card("rag_agent")
    assert card["name"] == "Financial RAG Agent"
    assert len(card["skills"]) == 2
    skill_ids = {s["id"] for s in card["skills"]}
    assert "sec_filing_retrieval" in skill_ids
    assert "earnings_summary" in skill_ids


def test_quant_card():
    card = _load_card("quant_agent")
    assert card["name"] == "Quant Analysis Agent"
    assert len(card["skills"]) == 1
    assert card["skills"][0]["id"] == "quant_analysis"


def test_sentiment_card():
    card = _load_card("sentiment_agent")
    assert card["name"] == "Sentiment Intelligence Agent"
    assert len(card["skills"]) == 1
    assert card["skills"][0]["id"] == "sentiment_analysis"


def test_all_cards_have_urls():
    for name in ["orchestrator_agent", "rag_agent", "quant_agent", "sentiment_agent"]:
        card = _load_card(name)
        assert "url" in card, f"{name} missing url"
        assert card["url"].startswith("http"), f"{name} url invalid"


def test_all_cards_have_capabilities():
    for name in ["orchestrator_agent", "rag_agent", "quant_agent", "sentiment_agent"]:
        card = _load_card(name)
        assert "capabilities" in card, f"{name} missing capabilities"
        assert "streaming" in card["capabilities"], f"{name} missing streaming flag"
