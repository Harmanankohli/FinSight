import json
import logging
from datetime import date

from crewai import Agent, Crew, Process, Task

from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)

from crewai import LLM as CrewLLM
from shared.config import ADK_MODEL, LLM_BASE_URL, LLM_API_KEY

_LLM = CrewLLM(model=ADK_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, temperature=0.3)


class MarketContextCrew:
    def __init__(self, mcp_wrapper: MCPClientWrapper):
        self._mcp = mcp_wrapper

    def build_crew(self, ticker: str, data: dict | None = None) -> Crew:
        data = data or {}
        macro = data.get("macro", {})
        peers = data.get("peers", {})
        sector = data.get("sector", "")
        industry = data.get("industry", "")

        m = macro.get("macro", {})
        s = macro.get("sectors", {})
        macro_summary = (
            f"Regime: {m.get('regime', 'unknown')} yield curve "
            f"(10Y={m.get('us10y', {}).get('value')}%, "
            f"spread={m.get('yield_curve_spread')}). "
            f"VIX={m.get('vix', {}).get('value')} "
            f"(5d change {m.get('vix', {}).get('change_5d_pct')}%). "
            f"DXY 5d change {m.get('dxy', {}).get('change_5d_pct')}%. "
            "Sector 1mo: " +
            ", ".join(f"{k}={v}%" for k, v in s.items() if isinstance(v, (int, float)))
        ) if m else "(macro data unavailable)"

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
        )

        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)

    async def analyze(self, ticker: str, precollected_data: dict | None = None) -> dict:
        crew = self.build_crew(ticker, data=precollected_data)
        try:
            from shared.llm_queue import llm_queue, Priority
            async with llm_queue.acquire(Priority.CRITICAL, "crewai-kickoff"):
                result = crew.kickoff()
            raw = result.raw if hasattr(result, "raw") else str(result)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"narrative": raw, "overall_signal": "neutral", "confidence_score": 0.5}
        except Exception as e:
            logger.exception("Market context crew failed for %s", ticker)
            return {
                "narrative": f"Analysis failed: {e}",
                "overall_signal": "neutral",
                "confidence_score": 0.0,
                "key_tailwinds": [],
                "key_headwinds": [],
            }
