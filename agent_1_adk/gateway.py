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

from .a2a_client import A2AClient, A2ADiscoverer
from .intent_parser import parse_query
from .report_generator import synthesize

logger = logging.getLogger(__name__)

_SEED_URLS = os.environ.get(
    "AGENT_SEED_URLS",
    "http://localhost:8002,http://localhost:8003,http://localhost:8004",
)

discoverer = A2ADiscoverer(seed_urls=[u.strip() for u in _SEED_URLS.split(",") if u.strip()])
a2a = A2AClient(timeout=45.0).with_discoverer(discoverer)
_discovered = False


async def _ensure_discovered():
    global _discovered
    if not _discovered:
        await discoverer.discover()
        _discovered = True
        logger.info("Discovered %d agents with %d skills",
                     len(discoverer.list_agents()), len(discoverer.list_skills()))


async def handle_query(request: Request):
    await _ensure_discovered()
    body = await request.json()
    query = body.get("query", "")
    portfolio = body.get("portfolio", [])
    risk_profile = body.get("risk_profile", "moderate")

    context = await parse_query(query, portfolio)
    ticker = context.ticker

    if not ticker:
        return JSONResponse({"error": "Could not determine ticker from query"}, status_code=400)

    tasks = {}

    skills = discoverer.list_skills()
    for skill_id in skills:
        if "sec_filing" in skill_id or "earnings" in skill_id:
            tasks["rag"] = a2a.query_rag(skill_id, ticker, query)
        elif "quant" in skill_id or "analysis" in skill_id:
            tasks["quant"] = a2a.query_quant(skill_id, ticker)
        elif "sentiment" in skill_id or "narrative" in skill_id:
            tasks["sentiment"] = a2a.query_sentiment(skill_id, ticker)

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
    await _ensure_discovered()
    return JSONResponse({
        "status": "ok",
        "discovered_agents": {
            url: card.get("name") for url, card in discoverer.list_agents().items()
        },
        "discovered_skills": list(discoverer.list_skills().keys()),
    })


from starlette.routing import Route

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
    Route("/query", endpoint=handle_query, methods=["POST"]),
    Route("/health", endpoint=health, methods=["GET"]),
]

app = Starlette(routes=routes)


@app.on_event("startup")
async def _startup():
    try:
        await discoverer.discover()
        global _discovered
        _discovered = True
        logger.info("Discovered %d agents with %d skills",
                     len(discoverer.list_agents()), len(discoverer.list_skills()))
    except Exception as e:
        logger.warning("Agent discovery failed (non-fatal): %s", e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8001)
