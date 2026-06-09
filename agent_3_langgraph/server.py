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

from shared.logging_config import logged, logged_sync, setup_file_logging
from shared.observability import init_langfuse, init_instrumentation, shutdown_langfuse

setup_file_logging("quant")
init_langfuse(service_name="quant_agent")
atexit.register(shutdown_langfuse)
init_instrumentation("quant")

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.generic_executor import GenericAgentExecutor
from .executor import QuantAgent

logger = logging.getLogger(__name__)


@logged(log_args=False, log_result=False)
async def health(request):
    return JSONResponse({"status": "ok", "agent": "quant"})


@logged()
async def release_evals(request):
    from shared.eval_gate import release_evals as _release
    n = await _release()
    return JSONResponse({"released": n})

host = os.environ.get("HOST", "localhost")

agent_card = AgentCard(
    name="Quant Analysis Agent",
    description="Computes quantitative risk metrics, financial analysis, behavioral signals, and peer comparisons using yfinance and LangGraph",
    version="2.0.0",
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(
            protocol_binding="JSONRPC", url=f"http://{host}:8003/a2a"
        )
    ],
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "application/json"],
    skills=[
        AgentSkill(
            id="quant_analysis",
            name="Quantitative Analysis",
            description="Compute Sharpe ratio, Beta, VaR, volatility, DCF valuation, stress tests, Monte Carlo simulation, and portfolio correlation",
            tags=["quantitative", "risk metrics", "sharpe", "beta", "dcf", "valuation", "monte carlo"],
            examples=[
                "Calculate risk metrics for NVDA",
                "Run a DCF valuation on AAPL",
                "Stress test MSFT under different market scenarios",
            ],
        ),
        AgentSkill(
            id="options_flow_analysis",
            name="Options Flow Analysis",
            description="Analyze put/call volume ratio, open interest imbalance, and unusual options activity to gauge market positioning",
            tags=["options", "put/call", "open interest", "flow", "positioning", "unusual activity"],
            examples=[
                "What's the options flow on NVDA?",
                "Is there unusual options activity in AAPL?",
                "Show me the put/call ratio for TSLA",
            ],
        ),
        AgentSkill(
            id="insider_transaction_analysis",
            name="Insider Transaction Analysis",
            description="Parse SEC Form 4 filings to detect insider buying/selling patterns, cluster activity, and net insider direction over 90 days",
            tags=["insider", "form 4", "sec", "insider buying", "insider selling"],
            examples=[
                "Are insiders buying AAPL?",
                "Show me recent insider transactions for MSFT",
                "Is there a cluster of insider selling at NVDA?",
            ],
        ),
        AgentSkill(
            id="positioning_signals",
            name="Positioning & Analyst Signals",
            description="Aggregate analyst consensus ratings, price target upside, short interest, and earnings surprise history into a composite positioning signal",
            tags=["analyst", "short interest", "price target", "consensus", "earnings surprise"],
            examples=[
                "What is analyst consensus on TSLA?",
                "What is the short interest on NVDA?",
                "Has AAPL been consistently beating earnings estimates?",
            ],
        ),
    ],
)

request_handler = DefaultRequestHandler(
    agent_executor=GenericAgentExecutor(QuantAgent()),
    task_store=SQLiteTaskStore(),
    agent_card=agent_card,
)

@logged()
async def _prewarm_llm():
    # Fire a minimal request so LM Studio loads model weights before the first real query.
    try:
        from langchain_openai import ChatOpenAI
        from shared.config import LLM_SUMMARY_MODEL, LLM_BASE_URL, LLM_API_KEY
        from shared.llm_queue import llm_queue, Priority
        llm = ChatOpenAI(model=LLM_SUMMARY_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.0, max_tokens=1)
        async with llm_queue.acquire(Priority.NORMAL, "quant-warmup"):
            await llm.ainvoke("ping")
        logger.info("LLM pre-warmed (%s)", LLM_SUMMARY_MODEL)
    except Exception as e:
        logger.warning("LLM warmup failed (non-fatal): %s", e)


routes = [Route("/health", health), Route("/release-evals", release_evals, methods=["POST"])]
routes.extend(create_agent_card_routes(agent_card))  # A2A agent card discovery
routes.extend(create_jsonrpc_routes(request_handler, "/a2a"))  # JSON-RPC task endpoints

app = Starlette(routes=routes, on_startup=[_prewarm_llm], debug=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
