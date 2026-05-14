import httpx, json, uuid, asyncio

async def t(name, url, query, timeout=15):
    body = {"jsonrpc":"2.0","method":"SendMessage","id":str(uuid.uuid4()),
        "params":{"message":{"messageId":str(uuid.uuid4()),"role":"ROLE_USER","parts":[{"text":query}]},
                  "metadata":{"ticker":"NVDA","period":"1mo"}}}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{url}/a2a", json=body, headers={"A2A-Version":"1.0"})
        d = r.json()
        if "error" in d:
            print(f"{name}: ERROR - {d['error'].get('message')}")
        else:
            task = d.get("result",{}).get("task",{})
            print(f"{name}: State={task.get('status',{}).get('state')}, Artifacts={len(task.get('artifacts',[]))}")
            for art in task.get("artifacts",[]):
                for p in art.get("parts",[]):
                    if "text" in p: print(f"  text: {p['text'][:100]}")
                    if "data" in p: print(f"  data keys: {list(p['data'].keys())[:4]}")

asyncio.run(t("RAG","http://localhost:8002","Analyze NVDA risks", 30))
asyncio.run(t("SENTIMENT","http://localhost:8004","Sentiment for NVDA", 60))
