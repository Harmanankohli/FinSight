import logging
import os

import uvicorn
from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from .executor import RAGAgentExecutor

logger = logging.getLogger(__name__)

host = os.environ.get("HOST", "localhost")
skill = AgentSkill(
    id="sec_filing_retrieval",
    name="SEC Filing Retrieval",
    description="Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings",
    input_modes=["text"],
    output_modes=["text", "data"],
)

earnings_skill = AgentSkill(
    id="earnings_summary",
    name="Earnings Summary",
    description="Summarizes earnings call transcripts",
    input_modes=["text"],
    output_modes=["text"],
)

agent_card = AgentCard(
    name="financial-rag-agent",
    description="Retrieves and analyzes financial documents using RAG",
    version="1.0.0",
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain", "application/json"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(protocol_binding="JSONRPC", url=f"http://{host}:8002/a2a")
    ],
    skills=[skill, earnings_skill],
)

request_handler = DefaultRequestHandler(
    agent_executor=RAGAgentExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
)

routes = []
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))

app = Starlette(routes=routes, debug=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8002)
