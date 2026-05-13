import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.mcp_client import MCPClient, MCPServerConfig

async def t():
    mcp = MCPClient(configs=[MCPServerConfig(name='fn', url='http://localhost:8025/sse')], max_retries=1)
    await mcp.connect_all()
    print('Tools:', list(mcp.get_available_tools().keys()))
    r = await mcp.call_tool_by_name('get_news_sentiment', {'ticker':'NVDA','limit':3})
    if hasattr(r, 'content'):
        for item in r.content:
            txt = item.text if hasattr(item, 'text') else str(item)
            d = json.loads(txt) if isinstance(txt, str) else txt
            if isinstance(d, dict):
                print(f'Articles: {d.get("total_articles")}, Sentiment: {d.get("sentiment_score")}')
                for a in d.get('articles', [])[:2]:
                    print(f'  - {a.get("source")}: {a.get("title")[:60]}')
    await mcp.disconnect_all()

asyncio.run(t())
