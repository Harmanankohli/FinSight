import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_1_adk.a2a_client import A2AClient


@pytest.fixture
def a2a_client():
    return A2AClient(timeout=5.0, max_retries=1)


def _mock_response(status_code=200, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    return mock


@pytest.mark.asyncio
async def test_send_task_success(a2a_client):
    mock_response = {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "id": "test-id",
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "name": "test_result",
                    "parts": [{"type": "data", "data": {"key": "value"}}],
                }
            ],
        },
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock(return_value=_mock_response(json_data=mock_response))

        result = await a2a_client.send_task(
            "http://localhost:8002", "sec_filing_retrieval", {"query": "test"}
        )

        assert result["result"]["status"]["state"] == "completed"
        assert result["result"]["artifacts"][0]["parts"][0]["data"]["key"] == "value"


@pytest.mark.asyncio
async def test_send_task_failure(a2a_client):
    mock_response = {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "id": "test-id",
            "status": {"state": "failed"},
            "artifacts": [{"name": "error", "parts": [{"type": "text", "text": "Analysis failed"}]}],
        },
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock(return_value=_mock_response(json_data=mock_response))

        with pytest.raises(RuntimeError, match="Analysis failed"):
            await a2a_client.send_task(
                "http://localhost:8002", "sec_filing_retrieval", {"query": "test"}
            )


@pytest.mark.asyncio
async def test_send_task_retry_on_timeout(a2a_client):
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock(side_effect=Exception("Connection error"))

        with pytest.raises(Exception, match="Connection error"):
            await a2a_client.send_task(
                "http://localhost:8002", "sec_filing_retrieval", {"query": "test"}
            )
        assert mock_client.post.call_count <= a2a_client.max_retries + 1


@pytest.mark.asyncio
async def test_query_rag(a2a_client):
    mock_response = {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "id": "test-id",
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "name": "NVDA_rag_result",
                    "parts": [{"type": "data", "data": {"summary": "Strong revenue growth"}}],
                }
            ],
        },
    }

    with patch.object(a2a_client, "send_task", new_callable=AsyncMock, return_value=mock_response):
        result = await a2a_client.query_rag("http://localhost:8002", "NVDA", "analyze")
        assert result == {"summary": "Strong revenue growth"}
