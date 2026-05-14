import json
import logging
import os
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.routes import create_agent_card_routes
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from .a2a_client import A2AClient, A2ADiscoverer
from .planner import parse_query, plan
from .report_generator import synthesize

from shared.config import (
    AGENT_REGISTRY_URL,
    AGENT_SEED_URLS,
    A2A_TIMEOUT,
    ORCHESTRATOR_PORT,
)

logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "localhost")

_card_path = Path(__file__).resolve().parent.parent / "agent_cards" / "orchestrator_agent.json"
with _card_path.open("r") as _f:
    _orchestrator_card = json.load(_f)


async def _discover_agents() -> A2ADiscoverer:
    discoverer = A2ADiscoverer(
        seed_urls=[u.strip() for u in AGENT_SEED_URLS.split(",") if u.strip()]
    )

    if AGENT_REGISTRY_URL:
        discoverer.with_mcp_registry(AGENT_REGISTRY_URL)

    await discoverer.discover()
    skills = discoverer.list_skills()
    logger.info("Discovered %d skills: %s", len(skills), list(skills.keys()))
    return discoverer


async def _execute_task(
    client: A2AClient, skill_id: str, params: dict
) -> dict:
    ticker = params.get("ticker", "")
    query = params.get("query", f"Analyze {ticker}")
    period = params.get("period", "5y")
    metadata = {"ticker": ticker, "period": period}

    logger.info("Executing skill '%s' for ticker %s", skill_id, ticker)
    result = await client.send_message(skill_id, query, metadata=metadata)
    return result


async def handle_query(request: Request):
    body = await request.json()
    query = body.get("query", "")
    portfolio = body.get("portfolio", [])
    risk_profile = body.get("risk_profile", "moderate")

    context = parse_query(query, portfolio)
    ticker = context.ticker
    if not ticker:
        return JSONResponse({"error": "Could not determine ticker from query"}, status_code=400)

    discoverer = await _discover_agents()
    if not discoverer.list_skills():
        return JSONResponse({"error": "No agents discovered"}, status_code=503)

    a2a_client = A2AClient(timeout=A2A_TIMEOUT).with_discoverer(discoverer)

    task_list = plan(context)
    logger.info(
        "Planned %d tasks for %s: %s",
        len(task_list.tasks), ticker,
        [t.skill_id for t in task_list.tasks],
    )

    results: dict[str, dict] = {}
    for task_def in task_list.tasks:
        try:
            skill_id = task_def.skill_id
            entry = discoverer.find_agent(skill_id)
            if not entry:
                logger.warning("No agent found for skill '%s', skipping", skill_id)
                continue

            result = await _execute_task(a2a_client, skill_id, task_def.params)

            if "sec_filing" in skill_id or "earnings" in skill_id:
                results["rag"] = result
            elif "quant" in skill_id:
                results["quant"] = result
            elif "sentiment" in skill_id:
                results["sentiment"] = result

            logger.info("Task '%s' completed successfully", skill_id)
        except Exception as e:
            logger.warning("Task '%s' failed: %s", task_def.skill_id, e)

    brief = synthesize(
        context,
        rag_data=results.get("rag"),
        quant_data=results.get("quant"),
        sentiment_data=results.get("sentiment"),
    )
    return JSONResponse(json.loads(brief.model_dump_json()))


async def health(_):
    return JSONResponse({"status": "ok"})


_skills = [
    AgentSkill(
        id=s["id"],
        name=s["name"],
        description=s["description"],
        input_modes=["text"],
        output_modes=["data"],
    )
    for s in _orchestrator_card.get("skills", [])
]

routes = [
    *create_agent_card_routes(AgentCard(
        name=_orchestrator_card["name"],
        description=_orchestrator_card["description"],
        version=_orchestrator_card.get("version", "1.0.0"),
        documentation_url=f"http://{HOST}:{ORCHESTRATOR_PORT}",
        default_input_modes=_orchestrator_card.get("defaultInputModes", ["text"]),
        default_output_modes=_orchestrator_card.get("defaultOutputModes", ["application/json"]),
        capabilities=AgentCapabilities(
            streaming=_orchestrator_card.get("capabilities", {}).get("streaming", False)
        ),
        skills=_skills,
    )),
    Route("/query", endpoint=handle_query, methods=["POST"]),
    Route("/health", endpoint=health, methods=["GET"]),
]

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=ORCHESTRATOR_PORT)
