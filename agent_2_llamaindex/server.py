import asyncio
import atexit
import logging
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from shared.a2a_store import SQLiteTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.logging_config import setup_file_logging
from shared.observability import init_langfuse, shutdown_langfuse

setup_file_logging("rag_agent")
init_langfuse(service_name="rag_agent")
atexit.register(shutdown_langfuse)
from openinference.instrumentation.llama_index import LlamaIndexInstrumentor

LlamaIndexInstrumentor().instrument()
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

StarletteInstrumentor().instrument()

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import RAGAgent
from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


async def health(request):
    # Health check for orchestrator-level monitoring and container orchestration probes
    return JSONResponse({"status": "ok", "agent": "rag"})

host = os.environ.get("HOST", "localhost")

agent_card = AgentCard(
    name="Financial RAG Agent",
    description="Retrieves and analyzes financial documents using RAG with ChromaDB and LM Studio",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC", url=f"http://{host}:8002/a2a"
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="sec_filing_retrieval",
            name="SEC Filing Retrieval",
            description="Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings with citation-backed insights",
            tags=["sec", "edgar", "filings", "financial documents"],
            examples=[
                "What are the key risks mentioned in NVDA's latest 10-K?",
                "Summarize AAPL's latest earnings report",
            ],
        ),
        AgentSkill(
            id="earnings_summary",
            name="Earnings Summary",
            description="Summarizes earnings call transcripts and forward guidance",
            tags=["earnings", "transcripts", "guidance"],
            examples=[
                "Summarize MSFT's latest earnings call",
                "What was NVDA's forward guidance?",
            ],
        ),
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=GenericAgentExecutor(RAGAgent()),
    task_store=SQLiteTaskStore(),
    agent_card=agent_card,
)

async def _prewarm():
    # Pre-load the embedding model at startup so the first query doesn't pay cold-start penalty
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, FinancialIndexManager)
    logger.info("Embedding model pre-warmed")


routes = [Route("/health", health)]  # Liveness check
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, on_startup=[_prewarm], debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
