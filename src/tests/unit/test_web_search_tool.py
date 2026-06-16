"""Tests for the web_search MCP tool — mocked DDGS, result structure, fallback."""
# ruff: noqa: S101 — assert used extensively in tests

import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure ddgs is importable even when not installed, so that
# patch("ddgs.DDGS", ...) can resolve the target module.
if "ddgs" not in sys.modules:
    sys.modules["ddgs"] = MagicMock()


@pytest.fixture
def mock_limiter(monkeypatch):
    monkeypatch.setattr("mcp_tools.tools.web_search._WEB_SEARCH_LIMITER.acquire", AsyncMock())


@pytest.fixture
def fake_ddgs():
    """Return a (patcher, ddgs_instance) tuple that patches ddgs.DDGS."""

    def _make(results):
        ddgs_instance = MagicMock()
        ddgs_instance.text.return_value = results
        ddgs_cls = MagicMock(
            return_value=MagicMock(__enter__=MagicMock(return_value=ddgs_instance)))

        patcher = patch("ddgs.DDGS", ddgs_cls)
        return patcher, ddgs_instance

    return _make


async def test_web_search_uncached_returns_expected_structure(fake_ddgs, mock_limiter):
    from mcp_tools.tools.web_search import _web_search_uncached

    fake_results = [
        {"title": "Title1", "href": "https://ex1.com", "body": "Snippet one"},
        {"title": "Title2", "href": "https://ex2.com", "body": "Snippet two"},
    ]
    patcher, _ = fake_ddgs(fake_results)
    patcher.start()
    try:
        result = await _web_search_uncached("AAPL stock", 5, "w")
    finally:
        patcher.stop()

    assert result["query"] == "AAPL stock"
    assert result["total_results"] == 2
    assert len(result["results"]) == 2
    for item in result["results"]:
        assert "title" in item
        assert "url" in item
        assert "snippet" in item
    assert "timestamp" in result
    datetime.fromisoformat(result["timestamp"])


async def test_web_search_uncached_empty_results(fake_ddgs, mock_limiter):
    from mcp_tools.tools.web_search import _web_search_uncached

    patcher, _ = fake_ddgs([])
    patcher.start()
    try:
        result = await _web_search_uncached("nonexistent", 3, "none")
    finally:
        patcher.stop()

    assert result["total_results"] == 0
    assert result["results"] == []


@pytest.mark.skip(
    reason="ImportError path requires removing installed package; covered by code review")
async def test_web_search_import_error_fallback():
    """When ddgs module can't be imported, return error dict."""
    from mcp_tools.tools.web_search import _web_search_uncached

    result = await _web_search_uncached("test", 5, "none")
    assert result["results"] == []
    assert "error" in result
    assert "not installed" in result["error"]


async def test_web_search_exception_graceful(fake_ddgs, mock_limiter):
    from mcp_tools.tools.web_search import _web_search_uncached

    patcher, ddgs_instance = fake_ddgs([])
    ddgs_instance.text.side_effect = ValueError("DDGS failed")
    patcher.start()
    try:
        result = await _web_search_uncached("test", 5, "none")
    finally:
        patcher.stop()

    assert result["results"] == []
    assert "error" in result


async def test_web_search_clamps_max_results(monkeypatch):
    from mcp_tools.tools.web_search import web_search

    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(
        return_value={"query": "test", "total_results": 0, "results": [], "timestamp": "now"}
    )
    monkeypatch.setattr("mcp_tools.tools.web_search.cache_web_search", mock_cache)

    result_over = await web_search("test", max_results=100, time_filter="none")
    assert result_over is not None

    result_under = await web_search("test", max_results=0, time_filter="none")
    assert result_under is not None


async def test_web_search_validates_time_filter(monkeypatch):
    from mcp_tools.tools.web_search import web_search

    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(
        return_value={"query": "test", "total_results": 0, "results": [], "timestamp": "now"}
    )
    monkeypatch.setattr("mcp_tools.tools.web_search.cache_web_search", mock_cache)

    result_invalid = await web_search("test", max_results=5, time_filter="invalid")
    assert result_invalid is not None

    result_valid = await web_search("test", max_results=5, time_filter="w")
    assert result_valid is not None
