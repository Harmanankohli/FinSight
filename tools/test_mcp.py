import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.mcp_client import MCPClient, MCPServerConfig

async def t():
    mcp = MCPClient(configs=[
        MCPServerConfig(name='yfinance', url='http://localhost:8010/sse'),
        MCPServerConfig(name='sec-edgar', url='http://localhost:8020/sse'),
        MCPServerConfig(name='reddit', url='http://localhost:8030/sse'),
        MCPServerConfig(name='python-runner', url='http://localhost:8040/sse'),
    ], max_retries=1)
    await mcp.connect_all()
    print('=== Discovered tools ===')
    for tool, server in mcp.get_available_tools().items():
        print(f'  {tool} (on {server})')

    print('\n=== Testing yfinance: get_prices(NVDA) ===')
    r = await mcp.call_tool_by_name('get_prices', {'ticker':'NVDA','period':'1mo','interval':'1d'})
    if hasattr(r, 'content'):
        for item in r.content:
            txt = item.text if hasattr(item, 'text') else str(item)
            d = json.loads(txt) if isinstance(txt, str) else txt
            if isinstance(d, dict):
                print(f'  Periods: {len(d.get("data", []))}, Ticker: {d.get("ticker")}')

    print('\n=== Testing sec-edgar: get_company_filings(NVDA) ===')
    r = await mcp.call_tool_by_name('get_company_filings', {'ticker':'NVDA','form_types':['10-K'],'limit':2})
    if hasattr(r, 'content'):
        for item in r.content:
            txt = item.text if hasattr(item, 'text') else str(item)
            print(f'  Filings: {txt[:300]}')

    print('\n=== Testing python-runner: execute_python ===')
    r = await mcp.call_tool_by_name('execute_python', {'code':'result = sum([1,2,3])'})
    if hasattr(r, 'content'):
        for item in r.content:
            txt = item.text if hasattr(item, 'text') else str(item)
            d = json.loads(txt) if isinstance(txt, str) else txt
            if isinstance(d, dict):
                print(f'  Success: {d.get("success")}, Result: {d.get("result")}')

    await mcp.disconnect_all()

asyncio.run(t())
