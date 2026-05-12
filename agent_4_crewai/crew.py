import json
import logging
from typing import Any

from crewai import Agent, Crew, Process, Task

from .mcp_tools import (
    FinancialNewsMCPTool,
    MCPClientWrapper,
    RedditMCPTool,
    SECInsiderMCPTool,
    TwitterMCPTool,
)

logger = logging.getLogger(__name__)

import os

_LLM_CONFIG = f"hosted_vllm/{os.environ.get('GROQ_MODEL', 'mixtral-8x7b-32768')}"


class SentimentIntelligenceCrew:
    def __init__(self, mcp_wrapper: MCPClientWrapper):
        self._mcp = mcp_wrapper

    def build_crew(self, ticker: str) -> Crew:
        social_agent = Agent(
            role="Social Media Sentiment Analyst",
            goal=f"Analyze retail investor sentiment for {ticker} across Reddit and Twitter",
            backstory="Expert in quantitative social sentiment analysis for financial markets",
            tools=[
                RedditMCPTool(self._mcp),
                TwitterMCPTool(self._mcp),
            ],
            llm=_LLM_CONFIG,
            verbose=True,
        )

        analyst_agent = Agent(
            role="Sell-Side Analyst Tracker",
            goal=f"Aggregate and analyze latest analyst upgrades, downgrades, and price targets for {ticker}",
            backstory="Former Goldman Sachs equity research associate with deep sector knowledge",
            tools=[FinancialNewsMCPTool(self._mcp)],
            llm=_LLM_CONFIG,
        )

        insider_agent = Agent(
            role="Insider Trading Monitor",
            goal=f"Review recent Form 4 filings for {ticker} executives and flag unusual activity",
            backstory="Compliance expert specializing in SEC Form 4 analysis and insider pattern detection",
            tools=[SECInsiderMCPTool(self._mcp)],
            llm=_LLM_CONFIG,
        )

        synthesis_agent = Agent(
            role="Investment Narrative Synthesizer",
            goal=f"Produce a coherent sentiment narrative combining all intelligence sources for {ticker}",
            backstory="Senior portfolio manager with 20 years of experience writing investment theses",
            llm=_LLM_CONFIG,
        )

        social_task = Task(
            description=(
                f"Scrape and score Reddit r/stocks, r/investing and Twitter/X posts mentioning "
                f"{ticker} in last 30 days. Return sentiment score (-1 to 1), volume, and key themes."
            ),
            agent=social_agent,
            expected_output="JSON with sentiment_score, post_volume, top_themes, notable_posts",
        )

        analyst_task = Task(
            description=(
                f"Find all analyst ratings changes for {ticker} in last 90 days. "
                f"Identify consensus rating, average price target, and most bullish/bearish arguments."
            ),
            agent=analyst_agent,
            expected_output="JSON with consensus_rating, avg_price_target, bull_case, bear_case",
        )

        insider_task = Task(
            description=(
                f"Review last 6 months of Form 4 filings for {ticker}. "
                f"Flag any unusual buy/sell patterns by C-suite or board members."
            ),
            agent=insider_agent,
            expected_output="JSON with insider_activity_summary, notable_transactions, signal (bullish/bearish/neutral)",
        )

        synthesis_task = Task(
            description=(
                "Synthesize all intelligence into a 3-paragraph investment narrative "
                "with an overall sentiment signal and confidence score."
            ),
            agent=synthesis_agent,
            context=[social_task, analyst_task, insider_task],
            expected_output=(
                "JSON with narrative, overall_signal, confidence_score (0-1), key_risks, key_catalysts"
            ),
        )

        return Crew(
            agents=[social_agent, analyst_agent, insider_agent, synthesis_agent],
            tasks=[social_task, analyst_task, insider_task, synthesis_task],
            process=Process.sequential,
            verbose=True,
        )

    async def analyze(self, ticker: str) -> dict:
        crew = self.build_crew(ticker)
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
