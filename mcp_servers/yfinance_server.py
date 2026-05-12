import logging

import yfinance as yf
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
app = FastMCP("yfinance-mcp")


@app.tool()
async def get_prices(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Fetch OHLCV price data for a ticker"""
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period, interval=interval)
    return {
        "ticker": ticker,
        "period": period,
        "data": hist.reset_index().to_dict(orient="records"),
    }


@app.tool()
async def get_financials(ticker: str) -> dict:
    """Fetch income statement, balance sheet, cash flow"""
    stock = yf.Ticker(ticker)
    return {
        "income_statement": stock.financials.to_dict() if stock.financials is not None else {},
        "balance_sheet": stock.balance_sheet.to_dict() if stock.balance_sheet is not None else {},
        "cash_flow": stock.cashflow.to_dict() if stock.cashflow is not None else {},
        "info": stock.info if stock.info else {},
    }


@app.tool()
async def get_options_chain(ticker: str, expiration: str | None = None) -> dict:
    """Fetch options chain for a ticker"""
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
