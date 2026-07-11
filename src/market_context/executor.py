"""Market Context Agent — macro regime analysis and competitive peer positioning.

Collects macro indicators, financial data, and web context for a ticker, then
delegates structured analysis to a CrewAI crew for narrative generation.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterable

from shared.base_agent import BaseAgent
from shared.logging_config import logged, logged_sync
from shared.mcp_client import get_shared_mcp
from shared.settings import EVAL_ENABLED
from shared.ticker_utils import resolve_and_validate_ticker

from .crew import MarketContextCrew
from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)


class MarketContextAgent(BaseAgent):
    """Agent that gathers market context data and produces a macro/peer analysis report.

    Fetches macro indicators, financials, web search results, and peer data for a
    given ticker, then kicks off a CrewAI crew to generate a structured narrative
    with an overall sentiment signal and confidence score.
    """

    @logged_sync(log_args=False, log_result=False)
    def __init__(self):
        super().__init__(
            agent_name="Market Context Agent",
            description="Provides macro regime analysis and competitive peer landscape positioning using CrewAI",  # noqa: E501
            content_types=["text", "application/json"],
        )

    @logged()
    async def _collect_data_parallel(self, ticker: str, mcp) -> dict:
        """Fetch macro, financials, web context, and peer data concurrently.

        Executes four stages of parallel MCP calls — macro indicators,
        primary financials, web search, and peer discovery — then aggregates
        results into a single dict consumed by the crew builder.
        """
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
            call(
                "web_search",
                {
                    "query": f"{ticker} stock market analysis outlook",
                    "max_results": 5,
                    "time_filter": "w",
                },
            ),
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
        macro_web = await call(
            "web_search",
            {
                "query": sector_query,
                "max_results": 5,
                "time_filter": "w",
            },
        )

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
            "primary_financials": info,
            "peers": peers,
            "web_context": web_context,
            "macro_web_context": macro_web,
        }

    @logged()
    async def analyze(self, ticker: str, query_text: str) -> dict:
        """Run full market context analysis for a ticker.

        Collects data via MCP calls, hands it to the CrewAI crew for
        narrative generation, and enriches the result with peer comparison
        metrics and retrieved context summaries.
        """
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
        result["_retrieved_contexts"] = self._extract_retrieved_contexts(data)
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
                    ("enterpriseToEbitda", "EV/EBITDA", False),
                ]:
                    if pinfo.get(key) is not None:
                        metrics[label] = f"{pinfo[key] * 100:.1f}%" if pct else f"{pinfo[key]:.1f}"
                if metrics:
                    peer_comparison.append({"ticker": sym, "metrics": metrics})
        result["peer_comparison"] = peer_comparison[:3]
        return result

    async def stream(self, query: str, context_id: str, task_id: str) -> AsyncIterable[dict]:
        """Stream a single market-context response for the given query.

        Part of the BaseAgent interface; delegates to _build_response
        and yields the result as a single chunk.
        """
        logger.info("MarketContextAgent.stream() called: query=%s...", query[:80])
        yield await self._build_response(query)

    @logged()
    async def _build_response(self, query: str) -> dict:
        """Resolve ticker from query, run analysis, and return a structured response.

        Handles ticker resolution, analysis execution, telemetry tracing,
        and optional evaluation deferral when EVAL_ENABLED is set.
        """
        async with self._telemetry_span("market-context-agent-stream", query) as (
            trace_ctx,
            span,
            trace_id,
        ):
            ticker, company = await resolve_and_validate_ticker(query)
            if not ticker:
                span.update(output={"error": company or "No ticker found"})
                return self._error_response(company or "Could not identify a stock ticker.")

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
                    from shared.runtime_eval import (
                        score_sentiment_response as _eval_sentiment_response,
                    )

                    defer_eval(
                        _eval_sentiment_response,
                        query,
                        narrative,
                        contexts,
                        trace_id,
                    )
                return self._data_response(result)
            except Exception as e:
                logger.exception("Market context analysis failed")
                span.update(output={"error": str(e)})
                return self._error_response(f"Market context analysis failed: {e}")

    @staticmethod
    def _extract_retrieved_contexts(data: dict) -> list[str]:
        """Flatten collected data into a list of human-readable context strings.

        Extracts macro regime, sector performance, peer financials, and web
        snippets from the raw data dict for downstream evaluation or logging.
        """
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
                for r in w.get("results") or []:
                    snippet = r.get("snippet", "")
                    if snippet:
                        contexts.append(f"[Web] {r.get('title', '')}: {snippet}")

        return contexts
