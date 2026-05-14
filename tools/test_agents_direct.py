"""Test each A2A agent directly via HTTP."""
import asyncio, httpx, json, uuid

async def test(name, url, query, ticker="NVDA"):
    body = {
        "jsonrpc": "2.0", "method": "SendMessage", "id": str(uuid.uuid4()),
        "params": {
            "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": query}]},
            "metadata": {"ticker": ticker, "period": "1mo"},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{url}/a2a", json=body, headers={"A2A-Version": "1.0"})
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                print(f"{name}: ERROR — {d['error'].get('message', 'unknown')}")
                return
            task = d.get("result", {}).get("task", {})
            state = task.get("status", {}).get("state", "unknown")
            artifacts = task.get("artifacts", [])
            print(f"{name}: state={state}, artifacts={len(artifacts)}")
            for art in artifacts:
                for p in art.get("parts", []):
                    if "data" in p:
                        keys = list(p["data"].keys())[:5]
                        print(f"  data keys: {keys}")
                        if "recommendation" in p["data"]:
                            print(f"  rec: {p['data']['recommendation']}")
                        if "overall_signal" in p["data"]:
                            print(f"  signal: {p['data']['overall_signal']}")
                    if "text" in p:
                        print(f"  text: {p['text'][:150]}")
    except Exception as e:
        print(f"{name}: EXCEPTION — {type(e).__name__}: {str(e)[:150]}")

async def main():
    await test("RAG", "http://localhost:8002", "Analyze NVDA risks")
    await test("QUANT", "http://localhost:8003", "Analyze NVDA")
    await test("SENTIMENT", "http://localhost:8004", "Sentiment for NVDA")

asyncio.run(main())
