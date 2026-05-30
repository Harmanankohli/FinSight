import logging
from datetime import datetime, timezone
from typing import Any

from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


class DocumentIngestionPipeline:
    def __init__(self, index_manager: FinancialIndexManager):
        self._index = index_manager

    def _make_doc(
        self,
        text: str,
        ticker: str,
        source: str,
        file_name: str,
        date: str | None = None,
    ) -> dict:
        return {
            "text": text,
            "ticker": ticker.upper(),
            "source": source,
            "file_name": file_name,
            "date": date or datetime.now(timezone.utc).isoformat(),
        }

    def ingest_sec_filing(
        self, ticker: str, filing: dict
    ) -> int:
        # Inserts into the "sec_filings" ChromaDB collection (10-K, 10-Q, 8-K documents)
        form = filing.get("form", "UNKNOWN")
        description = filing.get("description", "")
        url = filing.get("edgar_url", "")
        date = filing.get("filing_date", "")
        content = filing.get("content", "")
        if content and len(content) > 50:
            text = f"SEC Filing {form} for {ticker} filed {date}\n\nDescription: {description}\n\nContent:\n{content[:20000]}"
        else:
            text = f"SEC Filing {form} for {ticker}: {description}\nSource: {url}"
        doc = self._make_doc(text, ticker, "sec_edgar", f"{ticker}_{form}_{date}.html", date)
        return self._index.ingest_documents("sec_filings", [doc])

    def ingest_earnings_transcript(
        self, ticker: str, transcript: dict
    ) -> int:
        # Inserts into the "earnings" collection (earnings call transcripts)
        text = transcript.get("text", "")
        date = transcript.get("date", "")
        quarter = transcript.get("quarter", "")
        text = f"Earnings Call Transcript {quarter} for {ticker}:\n{text}"
        doc = self._make_doc(
            text, ticker, "earnings_transcript",
            f"{ticker}_earnings_{quarter}_{date}.txt", date,
        )
        return self._index.ingest_documents("earnings", [doc])

    def ingest_news_article(
        self, ticker: str, article: dict
    ) -> int:
        # Inserts into the "news" collection (financial news articles with sentiment)
        title = article.get("title", "")
        summary = article.get("summary", "")
        url = article.get("url", "")
        date = article.get("published_at", "")
        text = f"{title}\n{summary}\nSource: {url}"
        doc = self._make_doc(text, ticker, "financial_news", f"{ticker}_news_{date}.txt", date)
        return self._index.ingest_documents("news", [doc])

    def ingest_analyst_report(
        self, ticker: str, report: dict
    ) -> int:
        # Inserts into the "analyst_reports" collection (sell-side analyst research)
        title = report.get("title", "")
        content = report.get("content", "")
        analyst = report.get("analyst", "unknown")
        date = report.get("date", "")
        text = f"Analyst Report by {analyst}: {title}\n{content}"
        doc = self._make_doc(text, ticker, "analyst_report", f"{ticker}_report_{date}.pdf", date)
        return self._index.ingest_documents("analyst_reports", [doc])

    def ingest_sec_filings_batch(
        self, ticker: str, filings: list[dict]
    ) -> int:
        total = 0
        for filing in filings:
            total += self.ingest_sec_filing(ticker, filing)
        logger.info("Ingested %d SEC filings for %s", total, ticker)
        return total

    def ingest_from_mcp(
        self,
        ticker: str,
        sec_filings: list[dict] | None = None,
        earnings: list[dict] | None = None,
        news: list[dict] | None = None,
        analyst_reports: list[dict] | None = None,
    ) -> dict[str, int]:
        # Bulk ingestion router: dispatches each data source to its dedicated collection
        counts: dict[str, int] = {}
        if sec_filings:
            counts["sec_filings"] = self.ingest_sec_filings_batch(ticker, sec_filings)
        if earnings:
            for t in earnings:
                counts["earnings"] = counts.get("earnings", 0) + self.ingest_earnings_transcript(ticker, t)
        if news:
            for a in news:
                counts["news"] = counts.get("news", 0) + self.ingest_news_article(ticker, a)
        if analyst_reports:
            for r in analyst_reports:
                counts["analyst_reports"] = counts.get("analyst_reports", 0) + self.ingest_analyst_report(ticker, r)
        return counts
