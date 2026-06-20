"""Unit tests for trace context propagation — JSON-prefix protocol for Langfuse trace IDs across A2A boundaries."""

from shared.trace_context import (
    current_trace_id,
    extract_trace_context,
    extract_trace_ids,
    inject_trace_context,
)


def test_inject_extract_roundtrip():
    text = "Analyze AAPL for me"
    injected = inject_trace_context(text, "trace-123", "span-456")
    ctx, clean = extract_trace_context(injected)
    assert ctx is not None
    assert ctx["trace_id"] == "trace-123"
    assert ctx["parent_span_id"] == "span-456"
    assert clean == text


def test_extract_no_prefix_returns_none_and_original():
    text = "No trace context here"
    ctx, clean = extract_trace_context(text)
    assert ctx is None
    assert clean == text


def test_extract_ids_sets_contextvar():
    text = "Analyze NVDA"
    injected = inject_trace_context(text, "trace-xyz", "span-abc")
    trace_id, parent_span_id, clean = extract_trace_ids(injected)
    assert trace_id == "trace-xyz"
    assert parent_span_id == "span-abc"
    assert clean == text
    assert current_trace_id.get() == "trace-xyz"


def test_inject_skips_when_trace_id_empty():
    text = "Some query"
    assert inject_trace_context(text, "", "span-123") == text


def test_inject_skips_when_span_id_empty():
    text = "Some query"
    assert inject_trace_context(text, "trace-123", "") == text


def test_extract_ids_without_context_returns_none():
    trace_id, parent_span_id, clean = extract_trace_ids("plain text query")
    assert trace_id is None
    assert parent_span_id is None
    assert clean == "plain text query"


def test_separator_in_task_text_handled_correctly():
    # If the task text itself contains the separator, split(maxsplit=1) still works
    inner_sep = "Analyze AAPL\n<<<TASK>>>\nThis part is real"
    injected = inject_trace_context(inner_sep, "t1", "s1")
    ctx, clean = extract_trace_context(injected)
    assert ctx is not None
    # The first split gives us back the original text (including its separator)
    assert clean == inner_sep


def test_multiple_roundtrips_preserve_text():
    original = "What is Apple's valuation?"
    injected = inject_trace_context(original, "abc", "def")
    _, clean = extract_trace_context(injected)
    assert clean == original
    # Injecting again (e.g. orchestrator re-injects) produces nested prefix
    double_injected = inject_trace_context(injected, "abc2", "def2")
    ctx2, clean2 = extract_trace_context(double_injected)
    assert ctx2["trace_id"] == "abc2"
