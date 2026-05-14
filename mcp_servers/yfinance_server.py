import logging

import yfinance as yf
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
app = FastMCP("yfinance-mcp")


@app.tool()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price history data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        period: Time period for historical data. Options: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        interval: Data interval. Options: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo

    Returns:
        dict with keys: ticker (str), period (str), data (list of OHLCV records with Date, Open, High, Low, Close, Volume, Dividends, Stock Splits)
    """
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period, interval=interval)
    return {
        "ticker": ticker,
        "period": period,
        "data": hist.reset_index().to_dict(orient="records"),
    }


@app.tool()
async def get_financials(ticker: str) -> dict:
    """Fetch financial statements and company info for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)

    Returns:
        dict with keys: income_statement, balance_sheet, cash_flow (each a dict of financial data by fiscal year), info (company metadata including sector, market cap, PE ratio, etc.)
    """
    stock = yf.Ticker(ticker)
    return {
        "income_statement": stock.financials.to_dict() if stock.financials is not None else {},
        "balance_sheet": stock.balance_sheet.to_dict() if stock.balance_sheet is not None else {},
        "cash_flow": stock.cashflow.to_dict() if stock.cashflow is not None else {},
        "info": stock.info if stock.info else {},
    }


@app.tool()
async def get_options_chain(ticker: str, expiration: str | None = None) -> dict:
    """Fetch options chain data for a stock ticker.

    Args:
        ticker: Stock ticker symbol (e.g. NVDA, AAPL, MSFT)
        expiration: Optional option expiration date string (e.g. 2025-01-17). If omitted, returns available expiration dates.

    Returns:
        dict: If expiration provided, returns with keys: calls (list of call options), puts (list of put options). If no expiration, returns: expirations (list of available expiration date strings).
    """
    stock = yf.Ticker(ticker)
    if expiration:
        chain = stock.option_chain(expiration)
        return {
            "calls": chain.calls.to_dict(orient="records"),
            "puts": chain.puts.to_dict(orient="records"),
        }
    expirations = stock.options
    return {"expirations": list(expirations)}


_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host="0.0.0.0", port=8010)
