import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import ClientSession
from mcp.client.sse import sse_client

async def t():
    ctx = sse_client(url="http://localhost:8025/sse")
    streams = await ctx.__aenter__()
    s = await ClientSession(streams[0], streams[1]).__aenter__()
    await s.initialize()
    r = await s.call_tool("get_news_sentiment", {"ticker": "NVDA", "limit": 3})
    if hasattr(r, "content"):
        for item in r.content:
            txt = item.text if hasattr(item, "text") else str(item)
            d = json.loads(txt) if isinstance(txt, str) else txt
            if isinstance(d, dict):
                print(f"Articles: {d.get('total_articles')}, Sentiment: {d.get('sentiment_score')}")
                for a in d.get("articles", [])[:2]:
                    print(f"  {a.get('source')}: {a.get('title')[:80]}")
    await s.__aexit__(None, None, None)
    await ctx.__aexit__(None, None, None)

asyncio.run(t())
