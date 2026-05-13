import json
import logging
import os
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

import httpx as _httpx

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import SendMessageRequest, Role
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

from google.adk import Agent
from shared.config import (
    RAG_AGENT_URL, QUANT_AGENT_URL, SENTIMENT_AGENT_URL,
    A2A_TIMEOUT, ADK_MODEL, GROQ_API_KEY, LLM_BASE_URL,
)

os.environ.setdefault("OPENAI_API_KEY", GROQ_API_KEY)
os.environ.setdefault("OPENAI_BASE_URL", LLM_BASE_URL)

logger = logging.getLogger(__name__)


async def _call_skill(url: str, query: str, ticker: str = "", period: str = "5y") -> str:
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(A2A_TIMEOUT)) as h:
            card = await A2ACardResolver(h, url).get_agent_card()
            client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=h))
            meta = Struct()
            meta.update({"ticker": ticker, "period": period})
            req = SendMessageRequest(message=new_text_message(query, role=Role.ROLE_USER), metadata=meta)
            async for event in client.send_message(req):
                if hasattr(event, "task") and event.task and event.task.status.state == 3:
                    for art in event.task.artifacts:
                        for p in art.parts:
                            if p.data:
                                return json.dumps(MessageToDict(p.data))
                            if p.text:
                                return json.dumps({"text": p.text})
            await client.close()
    except Exception as e:
        return json.dumps({"error": str(e)})
    return "{}"


async def query_rag(ticker: str, query: str = "") -> str:
    return await _call_skill(RAG_AGENT_URL, query or f"Research {ticker}", ticker)


async def query_quant(ticker: str, period: str = "5y") -> str:
    return await _call_skill(QUANT_AGENT_URL, f"Analyze {ticker}", ticker, period)


async def query_sentiment(ticker: str) -> str:
    return await _call_skill(SENTIMENT_AGENT_URL, f"Sentiment for {ticker}", ticker)


root_agent = Agent(
    name="investment_orchestrator",
    model=ADK_MODEL,
    instruction=(
        "You are an investment research assistant. "
        "When the user asks about a stock (e.g. 'Should I invest in NVDA?'), "
        "call query_rag for SEC filings, query_quant for risk metrics, "
        "and query_sentiment for market sentiment. Then synthesize into "
        "a BUY/HOLD/SELL recommendation with rationale. "
        "For greetings, just respond conversationally."
    ),
    tools=[query_rag, query_quant, query_sentiment],
)
