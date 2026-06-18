import asyncio
import json
import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.logging_config import logged, logged_sync
from shared.mcp_client import get_shared_mcp
from shared.observability import get_langfuse_client
from shared.runtime_eval import score_sentiment_response as _eval_sentiment_response
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import resolve_and_validate_ticker
from shared.trace_context import extract_trace_ids

from .crew import MarketContextCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


class MarketContextAgent(BaseAgent):
    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Market Context Agent",
            description="Provides macro regime analysis and competitive peer landscape positioning using CrewAI",  # noqa: E501
            content_types=["text", "application/json"],
        )

    @logged()
    async def _collect_data_parallel(self, ticker: str, mcp) -> dict:
        async def call(tool, args):
            try:
                r = await mcp.call_tool_by_name(tool, args)
                if hasattr(r, "content"):
                    for item in r.content:
                        txt = item.text if hasattr(item, "text") else str(item)
                        try:
                            return json.loads(txt)
                        except (json.JSONDecodeError, TypeError):
                            return {"raw": txt[:500]}
                return {"raw": str(r)[:500]}
            except Exception as e:
                logger.warning("MCP call %s failed: %s", tool, e)
                return {"error": str(e)}

        # Step 1: macro indicators + primary financials + company web search in parallel
        macro, primary_fin, web_context = await asyncio.gather(
            call("get_macro_indicators", {}),
            call("get_financials", {"ticker": ticker}),
            call("web_search", {
                "query": f"{ticker} stock market analysis outlook",
                "max_results": 5,
                "time_filter": "w",
            }),
        )

        # Extract industry/sector from primary financials for crew context
        info = (
            primary_fin.get("info", {})
            if isinstance(primary_fin, dict) and not primary_fin.get("error")
            else {}
        )
        industry = info.get("industry", "")
        sector = info.get("sector", "")

        # Step 2: sector-level web search (needs sector from Step 1)
        sector_query = f"{sector or 'market'} sector outlook macro risks analyst commentary"
        macro_web = await call("web_search", {
            "query": sector_query,
            "max_results": 5,
            "time_filter": "w",
        })

        # Step 3: discover peers dynamically via Yahoo Finance recommendations API
        peers_result = await call("get_peers", {"ticker": ticker})
        peer_tickers = peers_result.get("peers", []) if isinstance(peers_result, dict) else []
        if not peer_tickers:
            logger.warning(
                "get_peers returned no results for %s — peer analysis will be skipped", ticker
            )

        # Step 4: peer financials — capped at 3 concurrent to avoid MCP/yfinance queue backup
        _sem = asyncio.Semaphore(3)

        async def _call_capped(t):
            async with _sem:
                return await call("get_financials", {"ticker": t})

        peer_fin_results = await asyncio.gather(
            *[_call_capped(p) for p in peer_tickers],
            return_exceptions=True,
        )

        peers = {}
        for sym, res in zip(peer_tickers, peer_fin_results, strict=False):
            if isinstance(res, Exception):
                peers[sym] = {"financials": {}}
            else:
                # get_financials returns {"info": {...}, "cash_flow": {...}, ...} directly
                fin_data = res if isinstance(res, dict) and not res.get("error") else {}
                peers[sym] = {"financials": fin_data}

        return {
            "macro": macro,
            "sector": sector,
            "industry": industry,
            "peers": peers,
            "web_context": web_context,
            "macro_web_context": macro_web,
        }

    @logged()
    async def analyze(self, ticker: str, query_text: str) -> dict:
        mcp = await get_shared_mcp()
        data = await self._collect_data_parallel(ticker, mcp)
        logger.info(
            "Collected market context for %s: macro=%s peers=%s",
            ticker,
            bool(data.get("macro")),
            list(data.get("peers", {}).keys()),
        )
        wrapper = MCPClientWrapper(mcp)
        crew_builder = MarketContextCrew(wrapper)
        result = await crew_builder.analyze(ticker, precollected_data=data)
        result["_retrieved_contexts"] = _extract_retrieved_contexts(data)
        peer_comparison = []
        for sym, pdata in data.get("peers", {}).items():
            pinfo = (pdata.get("financials") or {}).get("info", {})
            if pinfo:
                metrics = {}
                for key, label, pct in [
                    ("revenueGrowth", "Revenue Growth", True),
                    ("returnOnEquity", "ROE", True),
                    ("operatingMargins", "Operating Margin", True),
                    ("trailingPE", "P/E Ratio", False),
                ]:
                    if pinfo.get(key) is not None:
                        metrics[label] = f"{pinfo[key] * 100:.1f}%" if pct else f"{pinfo[key]:.1f}x"
                if metrics:
                    peer_comparison.append({"ticker": sym, "metrics": metrics})
        result["peer_comparison"] = peer_comparison[:3]
        return result

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        logger.info("MarketContextAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        trace_id, parent_span_id, query = extract_trace_ids(query)

        langfuse = get_langfuse_client()
        trace_ctx = (
            {"trace_id": trace_id, "parent_span_id": parent_span_id}
            if trace_id and parent_span_id
            else None
        )
        with langfuse.start_as_current_observation(
            as_type="span",
            name="market-context-agent-stream",
            input=query,
            trace_context=trace_ctx,
        ) as span:
            if trace_id:
                trace_ctx = {"trace_id": trace_id, "parent_span_id": span.id}

            ticker, company = await resolve_and_validate_ticker(query)
            if not ticker:
                span.update(output={"error": company or "No ticker found"})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": company or "Could not identify a stock ticker.",
                }

            try:
                result = await self.analyze(ticker, query)
                contexts = result.pop("_retrieved_contexts", [])
                span.update(
                    output={
                        "ticker": ticker,
                        "signal": result.get("overall_signal"),
                        "confidence": result.get("confidence_score"),
                    }
                )
                narrative = (
                    result.get("narrative")
                    or result.get("investment_narrative")
                    or result.get("analysis")
                    or json.dumps(result, indent=2)
                )
                if EVAL_ENABLED:
                    from shared.eval_gate import defer_eval

                    defer_eval(
                        _eval_sentiment_response,
                        query,
                        narrative,
                        contexts,
                        trace_id,
                    )
                return {
                    "response_type": "data",
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": result,
                }
            except Exception as e:
                logger.exception("Market context analysis failed")
                span.update(output={"error": str(e)})
                return {
                    "response_type": "text",
                    "is_task_complete": True,
                    "is_error": True,
                    "require_user_input": False,
                    "content": f"Market context analysis failed: {e}",
                }


@logged_sync()
def _extract_retrieved_contexts(data: dict) -> list[str]:
    contexts: list[str] = []

    macro = data.get("macro", {})
    m = (macro.get("macro") or {}) if isinstance(macro, dict) else {}
    if m:
        contexts.append(
            f"Macro regime: {m.get('regime', 'unknown')} yield curve, "
            f"10Y={m.get('us10y', {}).get('value')}%, "
            f"VIX={m.get('vix', {}).get('value')}, "
            f"DXY 5d change {m.get('dxy', {}).get('change_5d_pct')}%"
        )
        sectors = macro.get("sectors", {})
        if isinstance(sectors, dict):
            top = sorted(
                ((k, v) for k, v in sectors.items() if isinstance(v, (int, float))),
                key=lambda x: abs(x[1]),
                reverse=True,
            )[:3]
            if top:
                contexts.append("Sector 1mo: " + ", ".join(f"{k}={v}%" for k, v in top))

    for sym, pdata in (data.get("peers") or {}).items():
        pinfo = (pdata.get("financials") or {}).get("info", {})
        if pinfo:
            contexts.append(
                f"Peer {sym}: PE={pinfo.get('trailingPE')}, "
                f"RevGrowth={pinfo.get('revenueGrowth')}, "
                f"OpMargin={pinfo.get('operatingMargins')}, "
                f"MarketCap={pinfo.get('marketCap')}"
            )

    for key in ("web_context", "macro_web_context"):
        w = data.get(key)
        if isinstance(w, dict):
            for r in (w.get("results") or []):
                snippet = r.get("snippet", "")
                if snippet:
                    contexts.append(f"[Web] {r.get('title', '')}: {snippet}")

    return contexts
