# ruff: noqa: E402
import asyncio
import json
import logging
from datetime import date

from crewai import Agent, Crew, Process, Task

from shared.logging_config import logged, logged_sync

from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)

from crewai import LLM as CrewLLM

from shared.settings import ADK_MODEL, LLM_API_KEY, LLM_BASE_URL

_LLM = CrewLLM(model=ADK_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3)


class MarketContextCrew:
    @logged_sync(log_args=False, log_result=False)
    def __init__(self, mcp_wrapper: MCPClientWrapper):
        self._mcp = mcp_wrapper

    @logged_sync()
    def build_crew(self, ticker: str, data: dict | None = None) -> Crew:
        data = data or {}
        macro = data.get("macro", {})
        peers = data.get("peers", {})
        sector = data.get("sector", "")
        industry = data.get("industry", "")

        m = macro.get("macro", {})
        s = macro.get("sectors", {})
        macro_summary = (
            (
                f"Regime: {m.get('regime', 'unknown')} yield curve "
                f"(10Y={m.get('us10y', {}).get('value')}%, "
                f"spread={m.get('yield_curve_spread')}). "
                f"VIX={m.get('vix', {}).get('value')} "
                f"(5d change {m.get('vix', {}).get('change_5d_pct')}%). "
                f"DXY 5d change {m.get('dxy', {}).get('change_5d_pct')}%. "
                "Sector 1mo: "
                + ", ".join(f"{k}={v}%" for k, v in s.items() if isinstance(v, (int, float)))
            )
            if m
            else "(macro data unavailable)"
        )

        peer_lines = []
        for sym, pdata in peers.items():
            pinfo = (pdata.get("financials") or {}).get("info", {})
            peer_lines.append(
                f"  - {sym}: PE={pinfo.get('trailingPE')}, "
                f"RevGrowth={pinfo.get('revenueGrowth')}, "
                f"OpMargin={pinfo.get('operatingMargins')}, "
                f"MarketCap={pinfo.get('marketCap')}"
            )
        peer_summary = "\n".join(peer_lines) if peer_lines else "(no peer data)"

        context_data = (
            f"Target: {ticker} ({industry or sector})\n\n"
            f"MACRO REGIME:\n{macro_summary}\n\n"
            f"PEER LANDSCAPE ({len(peers)} peers):\n{peer_summary}"
        )

        agent = Agent(
            role="Market Context Analyst",
            goal=(
                f"Position {ticker} inside its current macro regime and competitive "
                f"landscape — does the environment favour or penalise this name right now?"
            ),
            backstory=(
                "Senior strategist who frames every investment in its macro and competitive "
                "context. Specialises in identifying regime shifts (rate cycles, vol spikes, "
                "sector rotation) and explaining how named peers create tailwinds or headwinds."
            ),
            llm=_LLM,
            verbose=False,
            allow_delegation=False,
            max_retry_limit=1,
        )

        from shared.agent_models import MarketContextOutput

        today = date.today().isoformat()
        task = Task(
            description=(
                f"Today's date: {today}. Given the macro regime and peer data below, "
                f"write a context narrative for {ticker}:\n\n"
                f"1. Macro alignment — does the current regime (yield curve, VIX, DXY, "
                f"sector rotation) favour this name?\n"
                f"2. Peer positioning — how does {ticker} stack up against the named peers "
                f"on growth, margins, valuation?\n"
                f"3. Net assessment — overall_signal (bullish/bearish/neutral) and "
                f"confidence_score (0-1).\n\n"
                f"{context_data}"
            ),
            agent=agent,
            expected_output=(
                "JSON with: narrative (2-3 paragraphs), macro_regime (string), "
                "relative_peer_positioning (string), overall_signal, confidence_score (0-1), "
                "key_tailwinds, key_headwinds."
            ),
            output_pydantic=MarketContextOutput,
        )

        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)

    @logged()
    async def analyze(self, ticker: str, precollected_data: dict | None = None) -> dict:
        from shared.agent_models import MarketContextOutput

        crew = self.build_crew(ticker, data=precollected_data)
        try:
            from shared.llm_queue import Priority, llm_queue

            async with llm_queue.acquire(Priority.CRITICAL, "crewai-kickoff"):
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, crew.kickoff)

            logger.info(
                "CrewAI result attrs: pydantic=%s, raw_type=%s, raw_preview=%.300s",
                type(getattr(result, "pydantic", None)),
                type(getattr(result, "raw", None)),
                str(getattr(result, "raw", ""))[:300],
            )

            if hasattr(result, "pydantic") and result.pydantic is not None:
                logger.info("Using pydantic path — signal=%s", result.pydantic.overall_signal)
                return result.pydantic.model_dump()

            raw = result.raw if hasattr(result, "raw") else str(result)
            try:
                parsed = MarketContextOutput.model_validate_json(raw)
                logger.info("Parsed raw JSON — signal=%s", parsed.overall_signal)
                return parsed.model_dump()
            except Exception as e1:
                logger.warning("model_validate_json failed: %s", e1)
                try:
                    parsed = MarketContextOutput.model_validate(json.loads(raw))
                    logger.info("Parsed json.loads — signal=%s", parsed.overall_signal)
                    return parsed.model_dump()
                except Exception as e2:
                    logger.warning("json.loads fallback failed: %s", e2)
                    return MarketContextOutput(
                        narrative=raw, overall_signal="neutral", confidence_score=0.5
                    ).model_dump()
        except Exception as e:
            logger.exception("Market context crew failed for %s", ticker)
            return MarketContextOutput(
                narrative=f"Analysis failed: {e}",
                overall_signal="neutral",
                confidence_score=0.0,
            ).model_dump()
