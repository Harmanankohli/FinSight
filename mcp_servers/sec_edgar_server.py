import logging
from html.parser import HTMLParser

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
app = FastMCP("sec-edgar-mcp")

SEC_HEADERS = {
    "User-Agent": "FinSight Research (contact@finsight.com)",
    "Accept-Encoding": "gzip, deflate",
}


class _EdgarClient:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cik_cache: dict[str, str] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=SEC_HEADERS, timeout=30.0)
        return self._client

    async def _lookup_cik(self, ticker: str) -> str:
        ticker = ticker.upper()
        if ticker in self._cik_cache:
            return self._cik_cache[ticker]

        c = await self._get_client()
        resp = await c.get("https://www.sec.gov/files/company_tickers.json")
        resp.raise_for_status()
        tickers = resp.json()
        for entry in tickers.values():
            if entry["ticker"] == ticker:
                cik = str(entry["cik_str"]).zfill(10)
                self._cik_cache[ticker] = cik
                return cik
        raise ValueError(f"Ticker {ticker} not found")

    async def get_company_filings(
        self, ticker: str, form_types: list[str] | None = None, limit: int = 10
    ) -> dict:
        cik = await self._lookup_cik(ticker)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        c = await self._get_client()
        for attempt in range(3):
            try:
                resp = await c.get(url)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    c = await self._get_client()
                else:
                    raise

        filings = data.get("filings", {}).get("recent", {})
        result = []
        for i in range(len(filings.get("form", []))):
            form = filings["form"][i]
            if form_types and form not in form_types:
                continue
            if len(result) >= limit:
                break
            result.append({
                "form": form,
                "filing_date": filings.get("filingDate", [""])[i],
                "description": filings.get("primaryDocument", [""])[i],
                "edgar_url": f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik}/{filings.get('accessionNumber', [''])[i].replace('-', '')}/{filings.get('primaryDocument', [''])[i]}",
            })

        return {"ticker": ticker, "cik": cik, "filings": result}

    async def full_text_search(self, query: str, ticker: str | None = None) -> dict:
        params: dict = {"q": query}
        if ticker:
            params["cik"] = await self._lookup_cik(ticker)
        c = await self._get_client()
        resp = await c.get(
            "https://efts.sec.gov/LATEST/search-index?dateRange=all",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for hit in data.get("hits", {}).get("hits", [])[:10]:
            src = hit.get("_source", {})
            results.append({
                "score": hit["_score"],
                "ticker": src.get("ticker", ""),
                "form": src.get("form", ""),
                "filing_date": src.get("filingDate", ""),
                "description": src.get("description", ""),
                "url": f"https://www.sec.gov{src.get('file_url', '')}",
            })
        return {"query": query, "results": results}

    async def close(self):
        await self._client.aclose()


_edgar = _EdgarClient()


@app.tool()
async def get_company_filings(
    ticker: str, form_types: list[str] | None = None, limit: int = 10
) -> dict:
    """Retrieve SEC filings for a company by ticker (10-K, 10-Q, 8-K, etc.)"""
    return await _edgar.get_company_filings(ticker, form_types, limit)


@app.tool()
async def full_text_search(query: str, ticker: str | None = None) -> dict:
    """Search SEC EDGAR full-text index for filings matching a query"""
    return await _edgar.full_text_search(query, ticker)


_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host="0.0.0.0", port=8020)
