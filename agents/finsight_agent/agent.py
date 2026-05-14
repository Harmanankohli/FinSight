import json
import logging
import os
import sys
import uuid
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

import httpx as _httpx

from google.adk import Agent
from shared.config import (
    RAG_AGENT_URL, QUANT_AGENT_URL, SENTIMENT_AGENT_URL,
    A2A_TIMEOUT, ADK_MODEL, LLM_BASE_URL,
)

os.environ.setdefault("OPENAI_API_BASE", LLM_BASE_URL)
os.environ.setdefault("OPENAI_API_KEY", "ollama")

logger = logging.getLogger(__name__)


async def _call_skill(url: str, query: str, ticker: str, period: str = "5y") -> str:
    timeout = 180.0
    task_id = str(uuid.uuid4())
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "id": task_id,
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": query}],
            },
            "metadata": {"ticker": ticker, "period": period},
        },
    }
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(timeout)) as c:
            resp = await c.post(
                f"{url}/a2a", json=body,
                headers={"A2A-Version": "1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            task = data.get("result", {}).get("task", {})
            if task.get("status", {}).get("state") == "TASK_STATE_FAILED":
                err = "Unknown error"
                for art in task.get("artifacts", []):
                    for p in art.get("parts", []):
                        if "text" in p: err = p["text"]
                return json.dumps({"error": err})
            for art in task.get("artifacts", []):
                for p in art.get("parts", []):
                    if "data" in p:
                        return json.dumps(p["data"])
                    if "text" in p:
                        return json.dumps({"text": p["text"]})
    except Exception as e:
        return json.dumps({"error": str(e)})
    return "{}"


async def query_rag(ticker: str) -> str:
    return await _call_skill(RAG_AGENT_URL, f"Research {ticker}", ticker)


async def query_quant(ticker: str) -> str:
    return await _call_skill(QUANT_AGENT_URL, f"Analyze {ticker}", ticker, "5y")


async def query_sentiment(ticker: str) -> str:
    return await _call_skill(SENTIMENT_AGENT_URL, f"Sentiment for {ticker}", ticker)


root_agent = Agent(
    name="investment_orchestrator",
    model=ADK_MODEL,
    instruction=(
        "You are an investment research assistant. "
        "When the user asks about a stock (e.g. 'Should I invest in NVDA?'), "
        "ALWAYS call ALL THREE tools: first query_rag, then query_quant, "
        "then query_sentiment. Each requires a 'ticker' parameter (e.g. NVDA, AAPL). "
        "After getting all three results, synthesize into a BUY/HOLD/SELL "
        "recommendation with a clear rationale. For greetings, just respond "
        "conversationally without calling any tools."
    ),
    tools=[query_rag, query_quant, query_sentiment],
)
