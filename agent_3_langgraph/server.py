import json
import logging
import os
from pathlib import Path

import uvicorn
from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from .executor import QuantAgentExecutor

logger = logging.getLogger(__name__)

host = os.environ.get("HOST", "localhost")

card_path = Path(__file__).resolve().parent.parent / "agent_cards" / "quant_agent.json"
with card_path.open("r") as f:
    card_data = json.load(f)

skills = [
    AgentSkill(
        id=s["id"],
        name=s["name"],
        description=s["description"],
        input_modes=["text"],
        output_modes=["data"],
    )
    for s in card_data.get("skills", [])
]

agent_card = AgentCard(
    name=card_data["name"],
    description=card_data["description"],
    version=card_data.get("version", "1.0.0"),
    default_input_modes=card_data.get("defaultInputModes", ["text/plain"]),
    default_output_modes=card_data.get("defaultOutputModes", ["text/plain"]),
    capabilities=AgentCapabilities(streaming=card_data.get("capabilities", {}).get("streaming", True)),
    supported_interfaces=[
        AgentInterface(protocol_binding="JSONRPC", url=f"http://{host}:8003/a2a")
    ],
    skills=skills,
)

request_handler = DefaultRequestHandler(
    agent_executor=QuantAgentExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
)

routes = []
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8003)
