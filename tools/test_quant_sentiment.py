"""Test Quant and Sentiment agents directly."""
import httpx, json, uuid, asyncio

async def test(name, url, query, ticker="NVDA"):
    body = {"jsonrpc":"2.0","method":"SendMessage","id":str(uuid.uuid4()),
        "params":{"message":{"messageId":str(uuid.uuid4()),"role":"ROLE_USER","parts":[{"text":query}]},
                  "metadata":{"ticker":ticker,"period":"1mo"}}}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{url}/a2a", json=body, headers={"A2A-Version":"1.0"})
            d = r.json()
            if "error" in d:
                print(f"{name}: ERROR - {d['error'].get('message','unknown')}")
            else:
                task = d.get("result",{}).get("task",{})
                s = task.get("status",{}).get("state")
                print(f"{name}: State={s}")
                for art in task.get("artifacts",[]):
                    for p in art.get("parts",[]):
                        if "data" in p: print(f"  Keys: {list(p['data'].keys())[:5]}")
                        if "text" in p: print(f"  Text: {p['text'][:200]}")
    except Exception as e:
        print(f"{name}: {type(e).__name__}")

async def main():
    await test("QUANT","http://localhost:8003","Analyze NVDA")
    await test("SENTIMENT","http://localhost:8004","Sentiment for NVDA")

asyncio.run(main())
