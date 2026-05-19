from shared.trace_context import inject_trace_context, extract_trace_context, extract_trace_ids
from shared.ticker_utils import extract_holdings

FAKE_TRACE_ID = "a" * 32
FAKE_PARENT_SPAN_ID = "b" * 16


def test_inject_and_extract_roundtrip():
    original_task = "Analyze NVDA SEC filings as of 2025-01-17."
    injected = inject_trace_context(original_task, FAKE_TRACE_ID, FAKE_PARENT_SPAN_ID)

    assert "<<<TASK>>>" in injected
    assert "_trace" in injected

    ctx, clean_task = extract_trace_context(injected)

    assert ctx is not None
    assert ctx["trace_id"] == FAKE_TRACE_ID
    assert ctx["parent_span_id"] == FAKE_PARENT_SPAN_ID
    assert clean_task == original_task


def test_extract_no_context_is_noop():
    raw_task = "Analyze NVDA SEC filings with no trace prefix."
    ctx, clean_task = extract_trace_context(raw_task)

    assert ctx is None
    assert clean_task == raw_task


def test_injected_task_preserves_multiline():
    task = "Line one.\nLine two.\nLine three with NVDA ticker."
    injected = inject_trace_context(task, FAKE_TRACE_ID, FAKE_PARENT_SPAN_ID)
    ctx, clean = extract_trace_context(injected)
    assert clean == task


def test_malformed_prefix_returns_none():
    task = "{{invalid_json}}\n<<<TASK>>>\nAnalyze NVDA."
    ctx, clean = extract_trace_context(task)
    assert ctx is None
    assert clean == task


def test_missing_trace_fields_returns_none():
    import json
    prefix = json.dumps({"_trace": {"wrong_field": "value"}})
    task = f"{prefix}\n<<<TASK>>>\nAnalyze NVDA."
    ctx, clean = extract_trace_context(task)
    assert ctx is None
    assert clean == task


def test_inject_empty_ids_returns_original():
    task = "Analyze AAPL."
    result = inject_trace_context(task, "", "")
    assert result == task


def test_extract_trace_ids_roundtrip():
    original_task = "Analyze NVDA SEC filings as of 2025-01-17."
    injected = inject_trace_context(original_task, FAKE_TRACE_ID, FAKE_PARENT_SPAN_ID)
    trace_id, parent_span_id, clean_task = extract_trace_ids(injected)
    assert trace_id == FAKE_TRACE_ID
    assert parent_span_id == FAKE_PARENT_SPAN_ID
    assert clean_task == original_task


def test_extract_trace_ids_no_context():
    raw_task = "Analyze NVDA SEC filings with no trace prefix."
    trace_id, parent_span_id, clean_task = extract_trace_ids(raw_task)
    assert trace_id is None
    assert parent_span_id is None
    assert clean_task == raw_task


def test_extract_holdings_portfolio_holds():
    query = "Should I buy ORCL? My portfolio holds AAPL, MSFT, GOOGL"
    holdings = extract_holdings(query, exclude_ticker="ORCL")
    assert holdings == ["AAPL", "MSFT", "GOOGL"]


def test_extract_holdings_colon_syntax():
    query = "Analyze NVDA. My portfolio: TSLA, AMZN, META"
    holdings = extract_holdings(query, exclude_ticker="NVDA")
    assert holdings == ["TSLA", "AMZN", "META"]


def test_extract_holdings_and_connector():
    query = "Should I invest in AAPL? I own MSFT and GOOGL"
    holdings = extract_holdings(query, exclude_ticker="AAPL")
    assert holdings == ["MSFT", "GOOGL"]


def test_extract_holdings_no_holdings_mentioned():
    query = "Should I buy NVDA?"
    holdings = extract_holdings(query)
    assert holdings == []


def test_extract_holdings_excludes_target_ticker():
    query = "Analyze AAPL. My portfolio holds AAPL, MSFT"
    holdings = extract_holdings(query, exclude_ticker="AAPL")
    assert holdings == ["MSFT"]
    assert "AAPL" not in holdings


def test_extract_holdings_current_positions():
    query = "Should I buy TSLA? My current holdings are JPM, BAC, WFC"
    holdings = extract_holdings(query, exclude_ticker="TSLA")
    assert holdings == ["JPM", "BAC", "WFC"]
