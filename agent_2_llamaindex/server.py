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
from shared.observability import init_langfuse, init_instrumentation, shutdown_langfuse

setup_file_logging("rag_agent")
init_langfuse(service_name="rag_agent")
atexit.register(shutdown_langfuse)
init_instrumentation("rag")

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import RAGAgent
from .hybrid_search import HybridSearchPipeline
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

_warm_index_manager: FinancialIndexManager | None = None
_warm_hybrid: HybridSearchPipeline | None = None


def _do_prewarm() -> tuple[FinancialIndexManager, HybridSearchPipeline]:
    # Heavy synchronous work — runs once in the executor at startup so the
    # first query doesn't pay the ~3-5s cold-start cost.
    import time

    t0 = time.perf_counter()
    mgr = FinancialIndexManager()  # loads HuggingFace embedding model + Chroma client
    t_embed = time.perf_counter() - t0

    # Force an encode so the embedding model's weights/tokenizer are fully loaded
    try:
        mgr.embed_model.get_text_embedding("warmup")
    except Exception as exc:  # non-fatal — warm-up is best-effort
        logger.debug("Embedding warm-up encode failed: %s", exc)

    # Pre-create the three known ChromaDB collections so the first per-collection
    # `get_or_create_collection` call doesn't block on disk I/O.
    t1 = time.perf_counter()
    for coll in ("sec_filings", "news", "earnings"):
        try:
            mgr._get_or_create_index(coll)
        except Exception as exc:
            logger.debug("Collection warm-up failed for %s: %s", coll, exc)
    t_chroma = time.perf_counter() - t1

    # Pre-load the CrossEncoder reranker used by the hybrid retrieval pipeline.
    t2 = time.perf_counter()
    pipe = HybridSearchPipeline()
    try:
        _ = pipe.reranker  # triggers CrossEncoder model download/load
    except Exception as exc:
        logger.debug("CrossEncoder warm-up failed: %s", exc)
    t_rerank = time.perf_counter() - t2

    logger.info(
        "RAG warm-up complete: embed=%.2fs chroma=%.2fs rerank=%.2fs total=%.2fs",
        t_embed, t_chroma, t_rerank, time.perf_counter() - t0,
    )
    return mgr, pipe


async def _prewarm():
    # Off-load to a thread executor so startup doesn't block the event loop.
    global _warm_index_manager, _warm_hybrid
    loop = asyncio.get_event_loop()
    try:
        _warm_index_manager, _warm_hybrid = await loop.run_in_executor(None, _do_prewarm)
    except Exception:
        logger.exception("RAG warm-up failed (non-fatal — first query will pay cold-start cost)")


routes = [Route("/health", health)]  # Liveness check
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, on_startup=[_prewarm], debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
