"""Test all MCP tools directly without LLM calls."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import ClientSession
from mcp.client.sse import sse_client

SERVERS = [
    ("yfinance (8010)", "http://localhost:8010/sse",
     [("get_prices", {"ticker": "AAPL", "period": "1mo", "interval": "1d"}),
      ("get_financials", {"ticker": "AAPL"}),
      ("get_options_chain", {"ticker": "AAPL"})]),
    ("sec-edgar (8020)", "http://localhost:8020/sse",
     [("get_company_filings", {"ticker": "AAPL", "form_types": "10-K", "limit": 2}),
      ("full_text_search", {"query": "revenue growth", "ticker": "AAPL"})]),
    ("financial-news (8025)", "http://localhost:8025/sse",
     [("get_news_sentiment", {"ticker": "AAPL", "limit": 3}),
      ("get_earnings_calendar", {"ticker": "AAPL"})]),
    ("python-runner (8040)", "http://localhost:8040/sse",
     [("execute_python", {"code": "result = sum([1,2,3])", "timeout": 10})]),
]

async def call_tool(url, name, args):
    try:
        ctx = sse_client(url=url)
        streams = await ctx.__aenter__()
        session = await ClientSession(streams[0], streams[1]).__aenter__()
        await session.initialize()
        result = await session.call_tool(name, args)
        output = []
        if hasattr(result, "content"):
            for item in result.content:
                txt = item.text if hasattr(item, "text") else str(item)
                try:
                    parsed = json.loads(txt)
                    output.append(json.dumps(parsed, indent=2)[:600])
                except (json.JSONDecodeError, TypeError):
                    output.append(txt[:600])
        await session.__aexit__(None, None, None)
        await ctx.__aexit__(None, None, None)
        return "\n".join(output)
    except Exception as e:
        return f"ERROR: {e}"

async def main():
    for server_name, url, tools in SERVERS:
        print(f"\n{'='*60}")
        print(f"SERVER: {server_name}")
        print(f"{'='*60}")
        for tool_name, args in tools:
            print(f"\n  TOOL: {tool_name}")
            print(f"  Args: {json.dumps(args)[:100]}")
            result = await call_tool(url, tool_name, args)
            for line in result.split("\n")[:8]:
                print(f"  {line}")
            if result.count("\n") > 8:
                print(f"  ... ({result.count(chr(10)) - 8} more lines)")

asyncio.run(main())
