import json
import logging
from datetime import date
from typing import Any

from crewai import Agent, Crew, Process, Task

from .mcp_tools import MCPClientWrapper

logger = logging.getLogger(__name__)

from crewai import LLM as CrewLLM
from shared.config import ADK_MODEL, LLM_BASE_URL

_LLM = CrewLLM(model=ADK_MODEL, base_url=LLM_BASE_URL, api_key="lmstudio", temperature=0.3)

_SYNTHESIS_BACKSTORY = "Senior portfolio manager with 20 years of experience writing investment theses"


class SentimentIntelligenceCrew:
    def __init__(self, mcp_wrapper: MCPClientWrapper):
        self._mcp = mcp_wrapper

    def build_crew(self, ticker: str, data: dict | None = None) -> Crew:
        news_summary = ""
        filings_summary = ""
        if data:
            news = data.get("news", {})
            if "error" not in news:
                articles = news.get("articles", [])
                news_summary = f"News articles found: {len(articles)}. Sentiment score: {news.get('sentiment_score', 'N/A')}. Top articles: "
                for a in articles[:3]:
                    news_summary += f"\n- {a.get('title', '')} (sentiment: {a.get('sentiment', 'N/A')})"
            else:
                news_summary = f"News unavailable: {news.get('error')}"
            filings = data.get("filings", {})
            if "error" not in filings:
                flist = filings.get("filings", [])
                filings_summary = f"SEC filings found: {len(flist)}. "
                for f in flist[:3]:
                    filings_summary += f"\n- {f.get('form', '')} filed {f.get('filing_date', '')}: {f.get('description', '')}"
            else:
                filings_summary = f"Filings unavailable: {filings.get('error')}"

        context_data = f"Data for {ticker}:\n\nNews:\n{news_summary}\n\nSEC Filings:\n{filings_summary}"

        agent = Agent(
            role="Investment Sentiment Analyst",
            goal=f"Analyze sentiment for {ticker} and produce an investment narrative",
            backstory=_SYNTHESIS_BACKSTORY,
            llm=_LLM,
            verbose=False,
            allow_delegation=False,
            max_retry_limit=1,
        )

        today = date.today().isoformat()
        task = Task(
            description=(
                f"Today's date: {today}. "
                f"Analyze the following data for {ticker} and produce:\n"
                f"1. Sentiment signal (bullish/bearish/neutral)\n"
                f"2. Key risks and catalysts\n"
                f"3. A 2-3 paragraph investment narrative\n"
                f"4. Overall confidence score (0-1)\n\n"
                f"{context_data}"
            ),
            agent=agent,
            expected_output=(
                "JSON with narrative, overall_signal, confidence_score (0-1), key_risks, key_catalysts"
            ),
        )

        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )

    async def analyze(self, ticker: str, precollected_data: dict | None = None) -> dict:
        crew = self.build_crew(ticker, data=precollected_data)
        try:
            result = crew.kickoff()
            raw = result.raw if hasattr(result, "raw") else str(result)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"narrative": raw, "overall_signal": "neutral", "confidence_score": 0.5}
        except Exception as e:
            logger.exception("Crew analysis failed for %s", ticker)
            return {
                "narrative": f"Analysis failed: {e}",
                "overall_signal": "neutral",
                "confidence_score": 0.0,
                "key_risks": [],
                "key_catalysts": [],
            }
