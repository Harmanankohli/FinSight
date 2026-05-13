import json
import logging
import os

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.server.routes import create_agent_card_routes
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    SendMessageRequest,
)
from google.protobuf.json_format import MessageToDict

from .intent_parser import parse_query
from .report_generator import synthesize

logger = logging.getLogger(__name__)

from shared.config import AGENT_SEED_URLS as _SEED_URLS


def _list_skills(card: AgentCard) -> dict[str, str]:
    return {s.id: s.description for s in card.skills}


async def _discover() -> dict[str, tuple]:
    agents: dict[str, tuple] = {}
    h = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    async with h:
        for url in _SEED_URLS.split(","):
            url = url.strip()
            if not url:
                continue
            try:
                card = await A2ACardResolver(h, url).get_agent_card()
                client = await create_client(
                    agent=card, client_config=ClientConfig(streaming=False, httpx_client=h),
                )
                agents[url] = (card, client)
                logger.info("Discovered '%s' at %s", card.name, url)
            except Exception as e:
                logger.warning("Failed to discover %s: %s", url, e)
    return agents


async def _call_agent(client, query: str, ticker: str, period: str = "5y") -> dict:
    from google.protobuf.struct_pb2 import Struct
    meta = Struct()
    meta.update({"ticker": ticker, "period": period})
    req = SendMessageRequest(message=new_text_message(query, role=1), metadata=meta)
    async for event in client.send_message(req):
        if hasattr(event, "task") and event.task:
            t = event.task
            if t.status.state == 3:
                for art in t.artifacts:
                    for p in art.parts:
                        if p.data:
                            return MessageToDict(p.data)
                        if p.text:
                            return {"text": p.text}
    return {}


async def handle_query(request: Request):
    body = await request.json()
    query = body.get("query", "")
    portfolio = body.get("portfolio", [])
    risk_profile = body.get("risk_profile", "moderate")

    context = await parse_query(query, portfolio)
    ticker = context.ticker
    if not ticker:
        return JSONResponse({"error": "Could not determine ticker"}, status_code=400)

    agents = await _discover()
    if not agents:
        return JSONResponse({"error": "No agents discovered"}, status_code=503)

    results = {}
    for url, (card, client) in agents.items():
        skills = _list_skills(card)
        try:
            for skill_id in skills:
                s = skill_id.lower()
                if "sec_filing" in s or "earnings" in s:
                    results["rag"] = await _call_agent(
                        client, query, ticker,
                    )
                elif "quant" in s or "analysis" in s:
                    results["quant"] = await _call_agent(
                        client, f"Analyze {ticker}", ticker, "5y",
                    )
                elif "sentiment" in s or "narrative" in s:
                    results["sentiment"] = await _call_agent(
                        client, f"Analyze sentiment for {ticker}", ticker,
                    )
        except Exception as e:
            logger.warning("Agent at %s failed: %s", url, e)

    brief = synthesize(
        context,
        rag_data=results.get("rag"),
        quant_data=results.get("quant"),
        sentiment_data=results.get("sentiment"),
    )
    return JSONResponse(json.loads(brief.model_dump_json()))


async def health(_):
    return JSONResponse({"status": "ok"})


routes = [
    *create_agent_card_routes(AgentCard(
        name="investment-orchestrator",
        description="Coordinates agents into an Investment Brief",
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

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8001)
