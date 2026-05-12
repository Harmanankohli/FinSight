import asyncio
import json
import logging
import os
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a.server.routes import create_agent_card_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from .a2a_client import A2AClient
from .intent_parser import parse_query
from .report_generator import synthesize

logger = logging.getLogger(__name__)

_RAG_URL = os.environ.get("AGENT_LLAMAINDEX_URL", "http://localhost:8002")
_QUANT_URL = os.environ.get("AGENT_LANGGRAPH_URL", "http://localhost:8003")
_SENTIMENT_URL = os.environ.get("AGENT_CREWAI_URL", "http://localhost:8004")

a2a = A2AClient(timeout=45.0)


async def handle_query(request: Request):
    body = await request.json()
    query = body.get("query", "")
    portfolio = body.get("portfolio", [])
    risk_profile = body.get("risk_profile", "moderate")

    context = await parse_query(query, portfolio)
    ticker = context.ticker

    if not ticker:
        return JSONResponse({"error": "Could not determine ticker from query"}, status_code=400)

    tasks = {}

    if _RAG_URL:
        tasks["rag"] = a2a.query_rag(_RAG_URL, ticker, query)

    if _QUANT_URL:
        tasks["quant"] = a2a.query_quant(_QUANT_URL, ticker)

    if _SENTIMENT_URL:
        tasks["sentiment"] = a2a.query_sentiment(_SENTIMENT_URL, ticker)

    results = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception as e:
            logger.warning("%s agent failed: %s", name, e)
            results[name] = None

    brief = synthesize(
        context,
        rag_data=results.get("rag"),
        quant_data=results.get("quant"),
        sentiment_data=results.get("sentiment"),
    )

    return JSONResponse(json.loads(brief.model_dump_json()))


from starlette.responses import FileResponse
from pathlib import Path


async def ui(_):
    return FileResponse(str(Path(__file__).resolve().parent.parent / "ui" / "test.html"))


async def health(_):
    return JSONResponse({
        "status": "ok",
        "rag": _RAG_URL,
        "quant": _QUANT_URL,
        "sentiment": _SENTIMENT_URL,
    })


routes = [
    *create_agent_card_routes(AgentCard(
        name="investment-orchestrator",
        description="Coordinates RAG, Quant, and Sentiment agents into an Investment Brief",
        version="1.0.0",
        documentation_url=f"http://{os.environ.get('HOST', 'localhost')}:8001",
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(
            id="investment_research",
            name="Investment Research",
            description="Answer investment queries with a complete research brief",
            input_modes=["text"],
            output_modes=["data"],
        )],
    )),
]

from starlette.routing import Route
routes.append(Route("/query", endpoint=handle_query, methods=["POST"]))
routes.append(Route("/health", endpoint=health, methods=["GET"]))

app = Starlette(routes=routes)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8001)
