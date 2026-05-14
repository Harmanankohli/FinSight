import pytest

from agent_1_adk.planner import extract_ticker, extract_risk_profile, extract_horizon, parse_query, plan


def test_extract_ticker():
    assert extract_ticker("Should I invest in NVDA?") == "NVDA"
    assert extract_ticker("What about AAPL?") == "AAPL"
    assert extract_ticker("no ticker here") is None


def test_extract_risk_profile():
    assert extract_risk_profile("conservative investor") == "conservative"
    assert extract_risk_profile("aggressive growth") == "aggressive"
    assert extract_risk_profile("moderate risk") == "moderate"


def test_extract_horizon():
    assert extract_horizon("short term play") == "short"
    assert extract_horizon("long term hold") == "long"
    assert extract_horizon("medium term") == "medium"


def test_parse_query_creates_context():
    ctx = parse_query("Analyze NVDA", ["AAPL"])
    assert ctx.ticker == "NVDA"
    assert ctx.user_risk_profile == "moderate"
    assert ctx.portfolio_holdings == ["AAPL"]
    assert ctx.session_id is not None


def test_plan_creates_task_list():
    ctx = parse_query("Research MSFT", [])
    task_list = plan(ctx)
    assert len(task_list.tasks) == 3
    assert task_list.tasks[0].skill_id == "sec_filing_retrieval"
    assert task_list.tasks[1].skill_id == "quant_analysis"
    assert task_list.tasks[2].skill_id == "sentiment_analysis"
    assert all(t.params.get("ticker") == "MSFT" for t in task_list.tasks)
