import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class A2AClient:
    def __init__(self, timeout: float = 30.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    async def send_task(
        self,
        agent_url: str,
        skill_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        task_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "id": task_id,
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": query}],
                },
                "metadata": {
                    **(metadata or {}),
                    "correlation_id": task_id,
                },
            },
        }

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{agent_url}/a2a",
                        json=body,
                        headers={"A2A-Version": "1.0"},
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    task = result.get("result", {}).get("task", {})
                    if task.get("status", {}).get("state") == "TASK_STATE_FAILED":
                        err_text = "Unknown error"
                        for art in task.get("artifacts", []):
                            for part in art.get("parts", []):
                                if "text" in part:
                                    err_text = part["text"]
                        raise RuntimeError(err_text)
                    return result
            except Exception as e:
                logger.warning(
                    "A2A call to %s/%s attempt %d/%d failed: %s",
                    agent_url, skill_id, attempt + 1, self.max_retries + 1, e,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                else:
                    raise

    def _extract_data(self, response: dict) -> Any:
        task = response.get("result", {}).get("task", {})
        for art in task.get("artifacts", []):
            for part in art.get("parts", []):
                if "data" in part:
                    data = part["data"]
                    if isinstance(data, dict):
                        return data
                    try:
                        return json.loads(str(data))
                    except (json.JSONDecodeError, TypeError):
                        return {"data": str(data)}
                if "text" in part:
                    return {"text": part["text"]}
        return {}

    async def query_rag(
        self, url: str, ticker: str, query_text: str, filing_types: list[str] | None = None
    ) -> Any:
        resp = await self.send_task(
            url, "sec_filing_retrieval", query_text,
            {"ticker": ticker, "filing_types": filing_types or ["10-K", "10-Q"]},
        )
        return self._extract_data(resp)

    async def query_quant(
        self, url: str, ticker: str, period: str = "5y"
    ) -> Any:
        resp = await self.send_task(
            url, "quant_analysis", f"Analyze {ticker} quantitative metrics",
            {"ticker": ticker, "period": period},
        )
        return self._extract_data(resp)

    async def query_sentiment(
        self, url: str, ticker: str
    ) -> Any:
        resp = await self.send_task(
            url, "sentiment_analysis", f"Analyze sentiment for {ticker}",
            {"ticker": ticker, "sources": ["reddit", "twitter"]},
        )
        return self._extract_data(resp)
