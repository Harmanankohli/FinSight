"""FastMCP/ADK server entry point for the RAG agent."""

# ruff: noqa: E402
import asyncio
import logging

from shared.bootstrap import bootstrap

_settings = bootstrap("rag")

from shared.observability import init_instrumentation

init_instrumentation("rag")

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from shared.agent_server import build_agent_app
from shared.logging_config import logged, logged_sync

from .executor import RAGAgent
from .hybrid_search import HybridSearchPipeline
from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)

# Agent card: keep in sync with agent_cards/financial_rag.json
agent_card = AgentCard(
    name="Financial RAG Agent",
    description="Retrieves and analyzes financial documents using RAG with ChromaDB and LM Studio",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC",
            url=f"http://{_settings.host}:{_settings.agent_port_rag}/a2a",
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="sec_filing_retrieval",
            name="SEC Filing Retrieval",
            description="Retrieves and analyzes SEC 10-K, 10-Q, 8-K filings with citation-backed insights",  # noqa: E501
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

_warm_index_manager: FinancialIndexManager | None = None
_warm_hybrid: HybridSearchPipeline | None = None


@logged_sync()
def _do_prewarm() -> tuple[FinancialIndexManager, HybridSearchPipeline]:
    import time

    t0 = time.perf_counter()
    mgr = FinancialIndexManager()
    t_embed = time.perf_counter() - t0

    try:
        mgr.embed_model.get_text_embedding("warmup")
    except Exception as exc:
        logger.debug("Embedding warm-up encode failed: %s", exc)

    t1 = time.perf_counter()
    for coll in ("sec_filings", "news", "earnings"):
        try:
            mgr._get_or_create_index(coll)
        except Exception as exc:
            logger.debug("Collection warm-up failed for %s: %s", coll, exc)
    t_chroma = time.perf_counter() - t1

    t2 = time.perf_counter()
    pipe = HybridSearchPipeline()
    try:
        _ = pipe.reranker
    except Exception as exc:
        logger.debug("CrossEncoder warm-up failed: %s", exc)
    t_rerank = time.perf_counter() - t2

    logger.info(
        "RAG warm-up complete: embed=%.2fs chroma=%.2fs rerank=%.2fs total=%.2fs",
        t_embed,
        t_chroma,
        t_rerank,
        time.perf_counter() - t0,
    )
    return mgr, pipe


@logged()
async def _prewarm():
    global _warm_index_manager, _warm_hybrid
    loop = asyncio.get_event_loop()
    try:
        _warm_index_manager, _warm_hybrid = await loop.run_in_executor(None, _do_prewarm)
    except Exception:
        logger.exception("RAG warm-up failed (non-fatal — first query will pay cold-start cost)")


app = build_agent_app(
    agent_card=agent_card,
    agent=RAGAgent(),
    service_name="rag",
    on_startup=[_prewarm],
    accept=frozenset({"service"}),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_settings.host, port=_settings.agent_port_rag)
