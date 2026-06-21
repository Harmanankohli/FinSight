"""Pipeline for ingesting financial documents (SEC filings, earnings, news, analyst reports) into ChromaDB collections."""  # noqa: E501

import logging
from datetime import UTC, datetime

from .index_manager import FinancialIndexManager

logger = logging.getLogger(__name__)


class DocumentIngestionPipeline:
    """Orchestrates ingestion of structured financial data into typed ChromaDB collections (sec_filings, earnings, news, analyst_reports)."""  # noqa: E501

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
        """Build a standardized document dict with ticker, source, file_name, date metadata for ChromaDB insertion."""  # noqa: E501
        return {
            "text": text,
            "ticker": ticker.upper(),
            "source": source,
            "file_name": file_name,
            "date": date or datetime.now(UTC).isoformat(),
        }

    def ingest_sec_filing(self, ticker: str, filing: dict) -> int:
        """Insert a single SEC filing (10-K, 10-Q, 8-K) into the sec_filings collection with form metadata and truncated content."""  # noqa: E501
        form = filing.get("form", "UNKNOWN")
        description = filing.get("description", "")
        url = filing.get("edgar_url", "")
        date = filing.get("filing_date", "")
        content = filing.get("content", "")
        if content and len(content) > 50:
            text = f"SEC Filing {form} for {ticker} filed {date}\n\nDescription: {description}\n\nContent:\n{content[:20000]}"  # noqa: E501
        else:
            text = f"SEC Filing {form} for {ticker}: {description}\nSource: {url}"
        doc = self._make_doc(text, ticker, "sec_edgar", f"{ticker}_{form}_{date}.html", date)
        return self._index.ingest_documents("sec_filings", [doc])

    def ingest_earnings_transcript(self, ticker: str, transcript: dict) -> int:
        """Insert an earnings call transcript into the earnings collection with quarter/date metadata."""  # noqa: E501
        text = transcript.get("text", "")
        date = transcript.get("date", "")
        quarter = transcript.get("quarter", "")
        text = f"Earnings Call Transcript {quarter} for {ticker}:\n{text}"
        doc = self._make_doc(
            text,
            ticker,
            "earnings_transcript",
            f"{ticker}_earnings_{quarter}_{date}.txt",
            date,
        )
        return self._index.ingest_documents("earnings", [doc])

    def ingest_news_article(self, ticker: str, article: dict) -> int:
        """Insert a financial news article with sentiment into the news collection."""  # noqa: E501
        title = article.get("title", "")
        summary = article.get("summary", "")
        url = article.get("url", "")
        date = article.get("published_at", "")
        text = f"{title}\n{summary}\nSource: {url}"
        doc = self._make_doc(text, ticker, "financial_news", f"{ticker}_news_{date}.txt", date)
        return self._index.ingest_documents("news", [doc])

    def ingest_analyst_report(self, ticker: str, report: dict) -> int:
        """Insert a sell-side analyst research report into the analyst_reports collection."""  # noqa: E501
        title = report.get("title", "")
        content = report.get("content", "")
        analyst = report.get("analyst", "unknown")
        date = report.get("date", "")
        text = f"Analyst Report by {analyst}: {title}\n{content}"
        doc = self._make_doc(text, ticker, "analyst_report", f"{ticker}_report_{date}.pdf", date)
        return self._index.ingest_documents("analyst_reports", [doc])

    def ingest_sec_filings_batch(self, ticker: str, filings: list[dict]) -> int:
        """Ingest multiple SEC filings for a ticker in batch and log the total count."""  # noqa: E501
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
        """Bulk ingestion router: dispatches each data source type to its dedicated ChromaDB collection and returns per-collection counts."""  # noqa: E501
        counts: dict[str, int] = {}
        if sec_filings:
            counts["sec_filings"] = self.ingest_sec_filings_batch(ticker, sec_filings)
        if earnings:
            for t in earnings:
                counts["earnings"] = counts.get("earnings", 0) + self.ingest_earnings_transcript(
                    ticker, t
                )
        if news:
            for a in news:
                counts["news"] = counts.get("news", 0) + self.ingest_news_article(ticker, a)
        if analyst_reports:
            for r in analyst_reports:
                counts["analyst_reports"] = counts.get(
                    "analyst_reports", 0
                ) + self.ingest_analyst_report(ticker, r)
        return counts
